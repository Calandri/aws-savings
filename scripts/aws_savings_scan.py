#!/usr/bin/env python3
"""aws-savings scan: a READ-ONLY inventory of the things that usually cost money for nothing.

Every AWS call made by this script is a describe / list / get / get-metric-* call.
It never creates, modifies, tags, stops or deletes anything. The output is a JSON file
(machine-readable, for the agent) and a Markdown report (human-readable) that list
*candidates* with evidence and a rough monthly estimate. Deciding what to cut is a
human job; see SKILL.md and references/ for the method, the tiers and the traps.

Cost of running it: Cost Explorer charges $0.01 per API request. A full run makes
roughly 15-30 Cost Explorer calls (about $0.30). Pass --skip-ce to make zero of them.
CloudWatch GetMetricData is charged per metric requested ($0.01 per 1,000 metrics);
a large account with thousands of Lambda functions costs a few cents.

Usage:
    python3 aws_savings_scan.py --profile myprofile
    python3 aws_savings_scan.py --profile myprofile --regions eu-central-1,us-east-1 --days 14
    python3 aws_savings_scan.py --skip-ce --out-dir ./out

Requires: Python 3.9+, boto3. Credentials from the usual chain (profile, env, SSO).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError
except ImportError:  # pragma: no cover
    print("boto3 is required: pip install boto3", file=sys.stderr)
    sys.exit(2)

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
TODAY = NOW.date()
YESTERDAY = TODAY - dt.timedelta(days=1)

BOTO_CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"}, connect_timeout=10, read_timeout=60)

# ---------------------------------------------------------------------------
# List prices used ONLY as a fallback for the "estimate" column. They are the
# public us-east-1 prices at the time of writing; other regions differ by about
# +-10%. Whenever the bill is available (Cost Explorer), the script derives the
# unit price from the bill instead (cost / quantity of the usage type) and marks
# the estimate basis as "bill". Everything else is marked "list".
# ---------------------------------------------------------------------------
LIST_PRICE = {
    "eip_hour": 0.005,                 # any public IPv4, idle or in use
    "nat_hour": 0.045,
    "nat_gb": 0.045,
    "ebs_gb_month": {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05},
    "ebs_snapshot_gb_month": 0.05,
    "lambda_snapstart_gb_s": 0.0000015,          # cached snapshot, per GB-second, per published version
    "lambda_provisioned_gb_s": 0.0000041667,
    "lambda_gb_s": 0.0000166667,
    "ecr_gb_month": 0.10,
    "s3_gb_month": {"StandardStorage": 0.023, "IntelligentTieringFAStorage": 0.023, "IntelligentTieringIAStorage": 0.0125,
                     "IntelligentTieringAAStorage": 0.004, "StandardIAStorage": 0.0125, "OneZoneIAStorage": 0.01,
                     "GlacierInstantRetrievalStorage": 0.004, "GlacierStorage": 0.0036, "DeepArchiveStorage": 0.00099,
                     "ReducedRedundancyStorage": 0.023},
    "s3_int_monitoring_per_1000_objects_month": 0.0025,
    "lightsail_snapshot_gb_month": 0.05,
    "waf_acl_month": 5.0,
    "waf_rule_month": 1.0,
    "waf_bot_control_month": 10.0,
    "secret_month": 0.40,
    "kms_key_month": 1.0,
    "cw_dashboard_month": 3.0,
    "vpc_interface_endpoint_hour_per_az": 0.01,
    "transfer_protocol_hour": 0.30,
    "alb_hour": 0.0225,
    "nlb_hour": 0.0225,
    "clb_hour": 0.025,
    "rds_db_insights_advanced_vcpu_hour": 0.0125,
    "apprunner_provisioned_gb_hour": 0.007,
    "aurora_io_per_million": 0.20,
    "aurora_storage_gb_month": {"aurora": 0.10, "aurora-iopt1": 0.225},
    "aurora_iopt_instance_multiplier": 1.30,
}
HOURS_MONTH = 730.0

DEPRECATED_RUNTIMES = {
    "python2.7", "python3.6", "python3.7", "python3.8", "python3.9",
    "nodejs", "nodejs4.3", "nodejs6.10", "nodejs8.10", "nodejs10.x", "nodejs12.x", "nodejs14.x", "nodejs16.x", "nodejs18.x",
    "ruby2.5", "ruby2.7", "ruby3.2", "go1.x", "dotnetcore2.1", "dotnetcore3.1", "dotnet6", "dotnet5.0", "java8", "provided",
}

S3_STORAGE_TYPES = [
    "StandardStorage", "IntelligentTieringFAStorage", "IntelligentTieringIAStorage", "IntelligentTieringAAStorage",
    "IntelligentTieringAIAStorage", "IntelligentTieringDAAStorage", "StandardIAStorage", "StandardIASizeOverhead",
    "OneZoneIAStorage", "GlacierInstantRetrievalStorage", "GlacierStorage", "GlacierObjectOverhead",
    "GlacierS3ObjectOverhead", "DeepArchiveStorage", "DeepArchiveObjectOverhead", "DeepArchiveS3ObjectOverhead",
    "ReducedRedundancyStorage",
]

CRON_NAME_HINT = re.compile(r"cron|schedul|worker|reconcile|nightly|daily|hourly|batch|job", re.I)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.findings: List[Dict[str, Any]] = []
        self.inventory: Dict[str, Any] = defaultdict(dict)
        self.skipped: List[Dict[str, str]] = []
        self.ce_calls = 0
        self.unit_prices: Dict[str, float] = {}   # usage-type -> $/unit derived from the bill

    def add(self, check: str, category: str, region: str, resource: str, detail: str,
            evidence: Optional[Dict[str, Any]] = None, est_month: Optional[float] = None,
            basis: Optional[str] = None, tier: str = "B", do: str = "", undo: str = "") -> None:
        with self.lock:
            self.findings.append({
                "check": check, "category": category, "region": region, "resource": resource,
                "detail": detail, "evidence": evidence or {},
                "est_usd_month": round(est_month, 2) if est_month is not None else None,
                "basis": basis, "tier_hint": tier, "do": do, "undo": undo,
            })

    def skip(self, check: str, region: str, err: Exception) -> None:
        with self.lock:
            self.skipped.append({"check": check, "region": region, "error": _short_err(err)})

    def inv(self, region: str, key: str, value: Any) -> None:
        with self.lock:
            self.inventory[region][key] = value


def _short_err(err: Exception) -> str:
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "ClientError")
        msg = err.response.get("Error", {}).get("Message", "")
        return f"{code}: {msg[:160]}"
    return f"{type(err).__name__}: {str(err)[:160]}"


def safe(report: Report, check: str, region: str, fn, *args, **kwargs):
    """Run a check; on any AWS error record it as skipped instead of aborting the scan.
    A transient connection error (DNS hiccup, reset) gets one retry after a short pause."""
    try:
        return fn(*args, **kwargs)
    except EndpointConnectionError:
        time.sleep(3)
        try:
            return fn(*args, **kwargs)
        except (ClientError, BotoCoreError) as e:
            report.skip(check, region, e)
    except (ClientError, BotoCoreError) as e:
        report.skip(check, region, e)
    except Exception as e:  # noqa: BLE001 - a bug in one check must not kill the whole scan
        report.skip(check, region, e)
    return None


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# CloudWatch helpers
# ---------------------------------------------------------------------------
def _period_for(days: int) -> int:
    """One single period covering the whole window (a true average, not an average of averages)."""
    p = days * 86400
    # CloudWatch wants multiples of 60 (<15d), 300 (<63d) or 3600 (>63d): a whole number of days satisfies all.
    return p


def metric_stat(cw, namespace: str, name: str, dims: Dict[str, str], days: int, stat: str) -> Optional[float]:
    start = NOW - dt.timedelta(days=days)
    r = cw.get_metric_statistics(
        Namespace=namespace, MetricName=name,
        Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=start, EndTime=NOW, Period=_period_for(days), Statistics=[stat],
    )
    pts = r.get("Datapoints", [])
    if not pts:
        return None
    if stat == "Sum":
        return float(sum(p["Sum"] for p in pts))
    if stat == "Maximum":
        return float(max(p["Maximum"] for p in pts))
    if stat == "Minimum":
        return float(min(p["Minimum"] for p in pts))
    return float(sum(p[stat] for p in pts) / len(pts))


def metric_data_batch(cw, queries: List[Dict[str, Any]], start: dt.datetime, end: dt.datetime) -> Dict[str, List[float]]:
    """Run get_metric_data in batches of 500 queries; return {id: values}."""
    out: Dict[str, List[float]] = {}
    for i in range(0, len(queries), 500):
        batch = queries[i:i + 500]
        token = None
        while True:
            kw = dict(MetricDataQueries=batch, StartTime=start, EndTime=end, ScanBy="TimestampDescending")
            if token:
                kw["NextToken"] = token
            r = cw.get_metric_data(**kw)
            for res in r.get("MetricDataResults", []):
                out.setdefault(res["Id"], []).extend(res.get("Values", []))
            token = r.get("NextToken")
            if not token:
                break
    return out


def q(id_: str, ns: str, name: str, dims: Dict[str, str], period: int, stat: str) -> Dict[str, Any]:
    return {"Id": id_, "ReturnData": True,
            "MetricStat": {"Metric": {"Namespace": ns, "MetricName": name,
                                      "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
                           "Period": period, "Stat": stat}}


def _mid(prefix: str, n: int) -> str:
    return f"{prefix}_{n}"


def age_days(ts) -> Optional[int]:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (NOW - ts).days


def gb(n: Optional[float]) -> float:
    return round((n or 0) / (1024 ** 3), 2)


# ---------------------------------------------------------------------------
# Cost Explorer (global, us-east-1)
# ---------------------------------------------------------------------------
USAGE_ONLY = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}


def ce_cost(ce, report: Report, start: dt.date, end: dt.date, granularity: str, group_key: Optional[str] = None,
            flt: Optional[Dict[str, Any]] = None, metrics=("UnblendedCost", "UsageQuantity")) -> List[Dict[str, Any]]:
    kw: Dict[str, Any] = {"TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
                          "Granularity": granularity, "Metrics": list(metrics)}
    if flt:
        kw["Filter"] = flt
    if group_key:
        kw["GroupBy"] = [{"Type": "DIMENSION", "Key": group_key}]
    rows = []
    token = None
    while True:
        if token:
            kw["NextPageToken"] = token
        r = ce.get_cost_and_usage(**kw)
        report.ce_calls += 1
        for t in r.get("ResultsByTime", []):
            day = t["TimePeriod"]["Start"]
            if t.get("Groups"):
                for g in t["Groups"]:
                    rows.append({"period": day, "key": g["Keys"][0],
                                 "cost": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                                 "qty": float(g["Metrics"].get("UsageQuantity", {}).get("Amount", 0))})
            else:
                tot = t.get("Total", {})
                rows.append({"period": day, "key": "TOTAL",
                             "cost": float(tot.get("UnblendedCost", {}).get("Amount", 0)),
                             "qty": float(tot.get("UsageQuantity", {}).get("Amount", 0))})
        token = r.get("NextPageToken")
        if not token:
            break
    return rows


def check_cost_explorer(session, report: Report, days: int) -> None:
    ce = session.client("ce", region_name="us-east-1", config=BOTO_CFG)
    inv: Dict[str, Any] = {}

    # 1. Daily usage-only spend, 30 days ending yesterday (the 1st of the month carries Tax + RI fees: filtered out).
    start30 = YESTERDAY - dt.timedelta(days=29)
    daily = safe(report, "ce.daily", "global", ce_cost, ce, report, start30, TODAY, "DAILY", None, USAGE_ONLY)
    if daily:
        inv["daily_usage_cost"] = [{"day": r["period"], "usd": round(r["cost"], 2)} for r in daily]
        vals = [r["cost"] for r in daily if r["period"] < YESTERDAY.isoformat()]
        if vals:
            inv["daily_median_usd"] = round(sorted(vals)[len(vals) // 2], 2)

    # 2. Per service: last 7 full days vs the 7 before (both ending yesterday), with a monthly-cadence flag.
    s7 = YESTERDAY - dt.timedelta(days=6)
    s14 = YESTERDAY - dt.timedelta(days=13)
    svc = safe(report, "ce.by_service", "global", ce_cost, ce, report, s14, TODAY, "DAILY", "SERVICE", USAGE_ONLY)
    if svc:
        cur: Dict[str, float] = defaultdict(float)
        prev: Dict[str, float] = defaultdict(float)
        maxday_cur: Dict[str, float] = defaultdict(float)
        maxday_prev: Dict[str, float] = defaultdict(float)
        for r in svc:
            if r["period"] >= s7.isoformat():
                cur[r["key"]] += r["cost"]
                maxday_cur[r["key"]] = max(maxday_cur[r["key"]], r["cost"])
            else:
                prev[r["key"]] += r["cost"]
                maxday_prev[r["key"]] = max(maxday_prev[r["key"]], r["cost"])
        table = []
        for k in sorted(set(cur) | set(prev), key=lambda x: -cur.get(x, 0)):
            c, p = cur.get(k, 0.0), prev.get(k, 0.0)
            # A service where one day carries most of a week is billed monthly (Route 53 zones, RI fees that
            # slipped through, licences): its week-over-week delta is noise, so it is flagged instead.
            monthly = (c > 1 and maxday_cur[k] > 0.6 * c) or (p > 1 and maxday_prev[k] > 0.6 * p)
            delta = None if (p == 0 or monthly) else round((c - p) / p * 100, 1)
            table.append({"service": k, "last7_usd": round(c, 2), "prev7_usd": round(p, 2),
                          "delta_pct": delta, "monthly_cadence": monthly})
        inv["service_7d"] = table[:40]

    # 3. Per region, last 7 days.
    reg = safe(report, "ce.by_region", "global", ce_cost, ce, report, s7, TODAY, "MONTHLY", "REGION", USAGE_ONLY)
    if reg:
        agg: Dict[str, float] = defaultdict(float)
        for r in reg:
            agg[r["key"]] += r["cost"]
        inv["region_7d"] = [{"region": k, "usd": round(v, 2)} for k, v in sorted(agg.items(), key=lambda x: -x[1])]

    # 4. Per usage type, 14 days daily: cost + quantity -> unit price from the bill, and the week-over-week movers.
    ut = safe(report, "ce.by_usage_type", "global", ce_cost, ce, report, s14, TODAY, "DAILY", "USAGE_TYPE", USAGE_ONLY)
    if ut:
        agg_c: Dict[str, float] = defaultdict(float)
        agg_q: Dict[str, float] = defaultdict(float)
        prev_c: Dict[str, float] = defaultdict(float)
        for r in ut:
            if r["period"] >= s7.isoformat():
                agg_c[r["key"]] += r["cost"]
                agg_q[r["key"]] += r["qty"]
            else:
                prev_c[r["key"]] += r["cost"]
        movers = []
        for k in set(agg_c) | set(prev_c):
            c, p = agg_c.get(k, 0.0), prev_c.get(k, 0.0)
            if abs(c - p) >= 3 and max(c, p) >= 5:
                movers.append({"usage_type": k, "last7_usd": round(c, 2), "prev7_usd": round(p, 2), "delta_usd": round(c - p, 2)})
        movers.sort(key=lambda m: -abs(m["delta_usd"]))
        inv["usage_type_movers"] = movers[:20]
        top = sorted(agg_c.items(), key=lambda x: -x[1])[:80]
        inv["usage_type_7d"] = [{"usage_type": k, "usd": round(v, 2), "qty": round(agg_q[k], 3)} for k, v in top]
        for k, c in agg_c.items():
            if agg_q[k] > 0 and c > 0:
                report.unit_prices[k] = c / agg_q[k]
        # Signals worth a line even before any describe call.
        watch = {
            "SnapStart-Cached": ("lambda", "SnapStart snapshot cache is billed per published version x memory; prune old versions, drop SnapStart on crons"),
            "Provisioned-Concurrency": ("lambda", "Provisioned concurrency is billed 24/7 whether invoked or not; check ProvisionedConcurrencyUtilization"),
            "IdleAddress": ("network", "Idle public IPv4 addresses (Elastic IPs not attached to anything)"),
            "NatGateway-Hours": ("network", "NAT gateway hourly fee; verify each one moves bytes"),
            "ChargedBackupUsage": ("rds", "RDS backup storage beyond the free quota: usually manual snapshots of databases that no longer exist"),
            "GMD-Metrics": ("cloudwatch", "GetMetricData volume: a dashboard/status page polling too often, or a monitoring SaaS"),
            "DatabaseInsights": ("rds", "Database Insights advanced mode, billed per vCPU-hour"),
            "Monitoring-Automation-INT": ("s3", "Intelligent-Tiering monitoring fee: only worth it on buckets whose objects are > 128 KB"),
            "Requests-Tier4": ("s3", "Lifecycle transition requests: a one-off spike after enabling a lifecycle rule is normal, a steady value means a Days:0 rule taxing every new object"),
            "DataTransfer-Out-Bytes": ("network", "Internet egress: look for a dataset re-downloaded by a training job, or a public bucket without a CDN"),
            "EBS:SnapshotUsage": ("ec2", "EBS snapshot storage; old manual snapshots and AMIs of long-gone machines"),
            "ProtocolHours": ("transfer", "AWS Transfer Family: $0.30/h per enabled protocol, 24/7, regardless of bytes"),
            "AMR-BotControl": ("waf", "WAF Bot Control subscription: make sure the web ACL is attached to something"),
            "TimedStorage-ByteHrs": ("s3", "S3 Standard storage volume"),
        }
        for k, c in top:
            for pat, (cat, why) in watch.items():
                if pat in k and c >= 0.5:
                    report.add("ce.usage_type_signal", cat, "global", k,
                               f"{why}. Last 7 days: ${c:.2f} ({agg_q[k]:.1f} units)",
                               {"usd_7d": round(c, 2), "qty_7d": round(agg_q[k], 2)},
                               est_month=c / 7 * 30, basis="bill", tier="info")

    # 5. Reservations and Savings Plans.
    first_of_prev = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    first_of_this = TODAY.replace(day=1)
    try:
        r = ce.get_reservation_utilization(TimePeriod={"Start": first_of_prev.isoformat(), "End": first_of_this.isoformat()},
                                           Granularity="MONTHLY")
        report.ce_calls += 1
        tot = (r.get("UtilizationsByTime") or [{}])[0].get("Total", {})
        inv["reservation_utilization_prev_month"] = {k: tot.get(k) for k in
                                                     ("UtilizationPercentage", "UnusedHours", "PurchasedHours", "NetRISavings", "UnusedUnits")}
        if tot.get("UtilizationPercentage") and float(tot["UtilizationPercentage"]) < 95:
            report.add("ce.ri_utilization", "reservations", "global", "reserved instances",
                       f"Reservation utilization last month {tot['UtilizationPercentage']}% (unused hours {tot.get('UnusedHours')})",
                       tot, tier="C")
    except (ClientError, BotoCoreError) as e:
        report.skip("ce.ri_utilization", "global", e)

    cov_rows = []
    for svc_name in ("Amazon Relational Database Service", "Amazon Elastic Compute Cloud - Compute",
                     "Amazon ElastiCache", "Amazon OpenSearch Service", "Amazon Redshift"):
        try:
            r = ce.get_reservation_coverage(
                TimePeriod={"Start": (YESTERDAY - dt.timedelta(days=13)).isoformat(), "End": TODAY.isoformat()},
                GroupBy=[{"Type": "DIMENSION", "Key": "INSTANCE_TYPE"}],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": [svc_name]}})
            report.ce_calls += 1
            for t in r.get("CoveragesByTime", []):
                for g in t.get("Groups", []):
                    itype = g.get("Attributes", {}).get("instanceType", "?")
                    ch = g.get("Coverage", {}).get("CoverageHours", {})
                    cc = g.get("Coverage", {}).get("CoverageCost", {})
                    row = {"service": svc_name, "instance_type": itype,
                           "coverage_pct": float(ch.get("CoverageHoursPercentage", 0) or 0),
                           "ondemand_hours": float(ch.get("OnDemandHours", 0) or 0),
                           "reserved_hours": float(ch.get("ReservedHours", 0) or 0),
                           "ondemand_cost_14d": float(cc.get("OnDemandCost", 0) or 0)}
                    if row["ondemand_cost_14d"] == 0 and row["ondemand_hours"] > 0:
                        # CoverageCost.OnDemandCost is frequently 0: take the hourly price of that instance type from the bill.
                        unit = next((v for k, v in report.unit_prices.items() if "InstanceUsage" in k and k.endswith(f":{itype}")), None)
                        if unit is None:
                            unit = next((v for k, v in report.unit_prices.items() if "BoxUsage" in k and k.endswith(f":{itype}")), None)
                        if unit:
                            row["ondemand_cost_14d"] = round(row["ondemand_hours"] * unit, 2)
                            row["cost_basis"] = "hours x unit price from the bill"
                    cov_rows.append(row)
                    if row["ondemand_hours"] >= 300 and row["ondemand_cost_14d"] >= 20:
                        month = row["ondemand_cost_14d"] / 14 * 30
                        report.add("ce.ri_coverage_gap", "reservations", "global",
                                   f"{svc_name.split(' - ')[0]} {itype}",
                                   f"{row['ondemand_hours']:.0f} on-demand hours in 14 days ({row['coverage_pct']:.0f}% covered): "
                                   f"${row['ondemand_cost_14d']:.0f}/14d at full price. A 1-year no-upfront reservation saves ~30%. "
                                   "Check first that the instance is not about to be retired or converted (a serverless conversion makes the RI useless).",
                                   row, est_month=month * 0.30, basis="bill", tier="B",
                                   do="Purchase a 1-year no-upfront RI only for steady 24/7 load; never 3 years on something you may retire.",
                                   undo="None: a reservation cannot be cancelled.")
        except (ClientError, BotoCoreError) as e:
            report.skip(f"ce.ri_coverage[{svc_name}]", "global", e)
    inv["reservation_coverage_14d"] = cov_rows

    try:
        r = ce.get_savings_plans_utilization(
            TimePeriod={"Start": (YESTERDAY - dt.timedelta(days=13)).isoformat(), "End": TODAY.isoformat()},
            Granularity="DAILY")
        report.ce_calls += 1
        rows = []
        for t in r.get("SavingsPlansUtilizationsByTime", []):
            u = t.get("Utilization", {})
            rows.append({"day": t["TimePeriod"]["Start"], "commitment": float(u.get("TotalCommitment", 0) or 0),
                         "used": float(u.get("UsedCommitment", 0) or 0), "unused": float(u.get("UnusedCommitment", 0) or 0),
                         "pct": float(u.get("UtilizationPercentage", 0) or 0)})
        inv["savings_plans_daily"] = rows
        if rows:
            unused = sum(x["unused"] for x in rows) / len(rows)
            if unused > 0.5:
                report.add("ce.sp_unused", "reservations", "global", "savings plans",
                           f"Savings Plans leave ${unused:.2f}/day of commitment unused (avg over 14 days). "
                           "Cannot be cancelled after 7 days: the lever is the renewal date and not buying RIs on a family an SP already covers.",
                           {"unused_per_day": round(unused, 2)}, est_month=unused * 30, basis="bill", tier="X")
        r2 = ce.get_savings_plans_utilization_details(
            TimePeriod={"Start": (YESTERDAY - dt.timedelta(days=13)).isoformat(), "End": TODAY.isoformat()})
        report.ce_calls += 1
        det = []
        for d in r2.get("SavingsPlansUtilizationDetails", []):
            a = d.get("Attributes", {})
            u = d.get("Utilization", {})
            det.append({"arn_suffix": d.get("SavingsPlanArn", "")[-12:], "type": a.get("SavingsPlansType"),
                        "family": a.get("InstanceFamily"), "region": a.get("Region"), "end": a.get("EndDateTime"),
                        "payment": a.get("PaymentOption"), "utilization_pct": float(u.get("UtilizationPercentage", 0) or 0),
                        "unused_14d": float(u.get("UnusedCommitment", 0) or 0)})
        inv["savings_plans_detail"] = det
    except (ClientError, BotoCoreError) as e:
        report.skip("ce.savings_plans", "global", e)

    # 6. Anomalies (only if a monitor exists; harmless otherwise).
    try:
        r = ce.get_anomalies(DateInterval={"StartDate": (YESTERDAY - dt.timedelta(days=29)).isoformat(),
                                           "EndDate": YESTERDAY.isoformat()}, MaxResults=50)
        report.ce_calls += 1
        inv["anomalies_30d"] = [{"start": a.get("AnomalyStartDate"), "end": a.get("AnomalyEndDate"),
                                 "impact_usd": a.get("Impact", {}).get("TotalImpact"),
                                 "root_causes": [{k: rc.get(k) for k in ("Service", "Region", "UsageType")}
                                                 for rc in a.get("RootCauses", [])]}
                                for a in r.get("Anomalies", [])]
    except (ClientError, BotoCoreError) as e:
        report.skip("ce.anomalies", "global", e)

    # 7. Purchase recommendation is deliberately NOT called: its lookback window includes what you just
    #    switched off. Run it by hand 30 days after the cuts have settled (see references/checks.md).
    report.inv("global", "cost_explorer", inv)


def check_commitments_calendar(session, report: Report, regions: List[str]) -> None:
    """Expiry dates of RIs and Savings Plans: the only moment an over-commitment becomes fixable."""
    cal = []
    try:
        sp = session.client("savingsplans", region_name="us-east-1", config=BOTO_CFG)
        for p in sp.describe_savings_plans(states=["active"]).get("savingsPlans", []):
            cal.append({"kind": "SavingsPlan", "type": p.get("savingsPlanType"), "family": p.get("ec2InstanceFamily"),
                        "region": p.get("region"), "commitment_per_hour": p.get("commitment"),
                        "payment": p.get("paymentOption"), "end": p.get("end"), "returnable_until": p.get("returnableUntil")})
    except (ClientError, BotoCoreError) as e:
        report.skip("savingsplans.describe", "global", e)
    for region in regions:
        try:
            ec2 = session.client("ec2", region_name=region, config=BOTO_CFG)
            for ri in ec2.describe_reserved_instances(Filters=[{"Name": "state", "Values": ["active"]}]).get("ReservedInstances", []):
                cal.append({"kind": "EC2-RI", "type": ri.get("InstanceType"), "count": ri.get("InstanceCount"),
                            "class": ri.get("OfferingClass"), "payment": ri.get("OfferingType"), "scope": ri.get("Scope"),
                            "region": region, "end": ri.get("End").isoformat() if ri.get("End") else None})
        except (ClientError, BotoCoreError) as e:
            report.skip("ec2.reserved_instances", region, e)
        try:
            rds = session.client("rds", region_name=region, config=BOTO_CFG)
            for ri in rds.describe_reserved_db_instances().get("ReservedDBInstances", []):
                if ri.get("State") != "active":
                    continue
                end = ri["StartTime"] + dt.timedelta(seconds=ri.get("Duration", 0))
                cal.append({"kind": "RDS-RI", "type": ri.get("DBInstanceClass"), "count": ri.get("DBInstanceCount"),
                            "payment": ri.get("OfferingType"), "multi_az": ri.get("MultiAZ"), "region": region,
                            "end": end.isoformat()})
        except (ClientError, BotoCoreError) as e:
            report.skip("rds.reserved_db_instances", region, e)
    cal.sort(key=lambda x: x.get("end") or "")
    report.inv("global", "commitments_calendar", cal)
    for c in cal:
        if c.get("kind") == "EC2-RI" and c.get("scope") == "Availability Zone":
            report.add("ri.zonal_scope", "reservations", c["region"], f"EC2 RI {c['type']}",
                       "Zonal reservation: it only applies to instances in that one AZ. If the instance moves AZ the RI is wasted. "
                       "Consider modify-reserved-instances to Scope=Region (free, reversible).",
                       c, tier="B", do="aws ec2 modify-reserved-instances --reserved-instances-ids <id> --target-configurations Scope=Region,InstanceCount=<n>,InstanceType=<type>")


# ---------------------------------------------------------------------------
# Regional checks
# ---------------------------------------------------------------------------
def price(report: Report, usage_type_fragment: str, fallback: float) -> (float, str):
    for k, v in report.unit_prices.items():
        if usage_type_fragment in k:
            return v, "bill"
    return fallback, "list"


def check_network(session, report: Report, region: str, days: int) -> None:
    ec2 = session.client("ec2", region_name=region, config=BOTO_CFG)
    cw = session.client("cloudwatch", region_name=region, config=BOTO_CFG)

    # NAT gateways: four independent traffic metrics; all zero over the window = nothing behind it.
    nats = ec2.describe_nat_gateways(Filter=[{"Name": "state", "Values": ["available"]}]).get("NatGateways", [])
    nat_hour, basis = price(report, "NatGateway-Hours", LIST_PRICE["nat_hour"])
    nat_rows = []
    for n in nats:
        nid = n["NatGatewayId"]
        name = next((t["Value"] for t in n.get("Tags", []) if t["Key"] == "Name"), "")
        m = {}
        for metric in ("BytesOutToDestination", "BytesInFromDestination", "BytesOutToSource", "ActiveConnectionCount"):
            m[metric] = safe(report, f"nat.metric.{metric}", region, metric_stat, cw, "AWS/NATGateway", metric,
                             {"NatGatewayId": nid}, days, "Sum") or 0.0
        eips = [a.get("PublicIp") for a in n.get("NatGatewayAddresses", []) if a.get("PublicIp")]
        row = {"id": nid, "name": name, "vpc": n.get("VpcId"), "subnet": n.get("SubnetId"), "created": str(n.get("CreateTime")),
               "eips": eips, **{k: int(v) for k, v in m.items()}}
        nat_rows.append(row)
        if all(v == 0 for v in m.values()):
            report.add("nat.zero_traffic", "network", region, f"{nid} {name}".strip(),
                       f"NAT gateway with zero bytes and zero connections over {days} days (4 independent metrics).",
                       row, est_month=nat_hour * HOURS_MONTH, basis=basis, tier="A",
                       do=f"Note subnet {n.get('SubnetId')} and the route tables pointing at it, then: aws ec2 delete-nat-gateway --region {region} --nat-gateway-id {nid}  (KEEP the Elastic IP for 30 days)",
                       undo=f"aws ec2 create-nat-gateway --region {region} --subnet-id {n.get('SubnetId')} --allocation-id <same allocation-id>, then restore the routes")
        elif m["BytesOutToDestination"] + m["BytesInFromDestination"] < 1e9:
            report.add("nat.low_traffic", "network", region, f"{nid} {name}".strip(),
                       f"NAT gateway moving < 1 GB in {days} days: check what it serves (a stopped instance? a dead Lambda VPC?).",
                       row, est_month=nat_hour * HOURS_MONTH, basis=basis, tier="C")
    report.inv(region, "nat_gateways", nat_rows)

    # Elastic IPs: unassociated ones are pure waste, but old addresses may live in a partner's allowlist.
    eip_hour, eb = price(report, "PublicIPv4:IdleAddress", LIST_PRICE["eip_hour"])
    addrs = ec2.describe_addresses().get("Addresses", [])
    idle = [a for a in addrs if not a.get("AssociationId") and not a.get("NetworkInterfaceId")]
    report.inv(region, "elastic_ips", {"total": len(addrs), "idle": len(idle)})
    for a in idle:
        name = next((t["Value"] for t in a.get("Tags", []) if t["Key"] == "Name"), "")
        report.add("eip.idle", "network", region, f"{a.get('PublicIp')} {name}".strip(),
                   "Elastic IP not associated with anything. Billed hourly since Feb 2024. Releasing is IRREVERSIBLE: "
                   "first check it is not in a customer/partner allowlist or DNS, then release after a month of silence.",
                   {"allocation_id": a.get("AllocationId"), "public_ip": a.get("PublicIp"), "name": name},
                   est_month=eip_hour * HOURS_MONTH, basis=eb, tier="B",
                   do=f"aws ec2 release-address --region {region} --allocation-id {a.get('AllocationId')}",
                   undo="None. The address is gone for good.")

    # Every public IPv4 is billed: count them by what they are attached to.
    enis = []
    token = None
    while True:
        kw = {"Filters": [{"Name": "association.public-ip", "Values": ["*"]}]}
        if token:
            kw["NextToken"] = token
        r = ec2.describe_network_interfaces(**kw)
        enis.extend(r.get("NetworkInterfaces", []))
        token = r.get("NextToken")
        if not token:
            break
    by_type: Dict[str, int] = defaultdict(int)
    for e in enis:
        desc = (e.get("Description") or "").lower()
        t = e.get("InterfaceType", "interface")
        if t == "nat_gateway" or "nat gateway" in desc:
            kind = "nat_gateway"
        elif "elb" in desc:
            kind = "load_balancer_node"
        elif "rdsnetworkinterface" in desc:
            kind = "rds_public_endpoint"
        elif "vpc endpoint" in desc or t == "vpc_endpoint":
            kind = "vpc_endpoint"
        elif "ecs" in desc or "fargate" in desc:
            kind = "ecs_task"
        elif e.get("Attachment", {}).get("InstanceId"):
            kind = "ec2_instance"
        elif "transfer" in desc:
            kind = "transfer_family"
        else:
            kind = f"other:{t}"
        by_type[kind] += 1
    total_pub = len(enis)
    report.inv(region, "public_ipv4_by_attachment", dict(by_type))
    if total_pub:
        report.add("ipv4.inventory", "network", region, f"{total_pub} public IPv4 in use",
                   "Every public IPv4 costs the same hourly fee whether idle or in use. " + ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())) +
                   ". Candidates: RDS public endpoints (make private if nobody connects from outside), spare ALB, ECS tasks that could sit behind a NAT.",
                   dict(by_type), est_month=total_pub * eip_hour * HOURS_MONTH, basis=eb, tier="info")

    # VPC endpoints: gateway endpoints for S3/DynamoDB are free; interface endpoints are not.
    vpce = ec2.describe_vpc_endpoints().get("VpcEndpoints", [])
    gw_s3_vpcs = {v["VpcId"] for v in vpce if v.get("VpcEndpointType") == "Gateway" and v.get("ServiceName", "").endswith(".s3")}
    iface = [v for v in vpce if v.get("VpcEndpointType") == "Interface"]
    report.inv(region, "vpc_endpoints", {"gateway": len(vpce) - len(iface), "interface": len(iface)})
    nat_vpcs = {n.get("VpcId") for n in nats}
    for vpc in sorted(nat_vpcs - gw_s3_vpcs):
        report.add("vpce.missing_s3_gateway", "network", region, vpc,
                   "VPC with a NAT gateway but no S3 gateway endpoint: S3 traffic from private subnets pays NAT data processing. "
                   "The gateway endpoint is free. Caveat: bucket policies that allow only the NAT's IP will start returning 403.",
                   {"vpc": vpc}, tier="B",
                   do=f"aws ec2 create-vpc-endpoint --region {region} --vpc-id {vpc} --service-name com.amazonaws.{region}.s3 --route-table-ids <private route tables>",
                   undo="aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <id>")
    if iface:
        enis_iface = sum(len(v.get("NetworkInterfaceIds", [])) for v in iface)
        report.add("vpce.interface_cost", "network", region, f"{len(iface)} interface endpoints",
                   f"{enis_iface} endpoint ENIs at ~$0.01/h each (~${enis_iface * 0.01 * HOURS_MONTH:.0f}/month). "
                   "Each interface endpoint costs about $22/month per AZ: usually MORE than the NAT data it saves. Keep only the ones that exist for security, not for cost.",
                   {"services": sorted({v.get("ServiceName", "").split(".")[-1] for v in iface})},
                   est_month=enis_iface * LIST_PRICE["vpc_interface_endpoint_hour_per_az"] * HOURS_MONTH, basis="list", tier="info")

    # Load balancers.
    try:
        elbv2 = session.client("elbv2", region_name=region, config=BOTO_CFG)
        lbs = elbv2.describe_load_balancers().get("LoadBalancers", [])
        lb_rows = []
        for lb in lbs:
            arn = lb["LoadBalancerArn"]
            suffix = arn.split(":loadbalancer/")[-1]
            kind = lb.get("Type")
            if kind == "application":
                reqs = safe(report, "alb.requests", region, metric_stat, cw, "AWS/ApplicationELB", "RequestCount",
                            {"LoadBalancer": suffix}, min(days, 30), "Sum")
                hourly = LIST_PRICE["alb_hour"]
            elif kind == "network":
                reqs = safe(report, "nlb.flows", region, metric_stat, cw, "AWS/NetworkELB", "ActiveFlowCount",
                            {"LoadBalancer": suffix}, min(days, 30), "Sum")
                hourly = LIST_PRICE["nlb_hour"]
            else:
                reqs, hourly = None, LIST_PRICE["alb_hour"]
            row = {"name": lb.get("LoadBalancerName"), "type": kind, "scheme": lb.get("Scheme"),
                   "created": str(lb.get("CreatedTime")), "azs": len(lb.get("AvailabilityZones", [])), "requests_window": reqs}
            lb_rows.append(row)
            if reqs is not None and reqs == 0:
                report.add("lb.zero_requests", "network", region, f"{kind} {lb.get('LoadBalancerName')}",
                           f"Load balancer with zero requests/flows in {min(days, 30)} days. Hourly fee + one public IPv4 per AZ.",
                           row, est_month=hourly * HOURS_MONTH + len(lb.get("AvailabilityZones", [])) * eip_hour * HOURS_MONTH,
                           basis="list", tier="B",
                           do=f"Save the listeners/target groups config, then aws elbv2 delete-load-balancer --load-balancer-arn <arn>",
                           undo="Recreate from the saved config (the DNS name changes).")
        report.inv(region, "load_balancers", lb_rows)
        tgs = elbv2.describe_target_groups().get("TargetGroups", [])
        orphan_tg = [t["TargetGroupName"] for t in tgs if not t.get("LoadBalancerArns")]
        if orphan_tg:
            report.inv(region, "orphan_target_groups", orphan_tg)
    except (ClientError, BotoCoreError) as e:
        report.skip("elbv2", region, e)
    try:
        elb = session.client("elb", region_name=region, config=BOTO_CFG)
        clbs = elb.describe_load_balancers().get("LoadBalancerDescriptions", [])
        for lb in clbs:
            reqs = safe(report, "clb.requests", region, metric_stat, cw, "AWS/ELB", "RequestCount",
                        {"LoadBalancerName": lb["LoadBalancerName"]}, min(days, 30), "Sum")
            if reqs is None or reqs == 0:
                report.add("lb.classic_idle", "network", region, f"classic {lb['LoadBalancerName']}",
                           f"Classic load balancer with no requests in {min(days, 30)} days (or no metric at all).",
                           {"created": str(lb.get("CreatedTime")), "instances": len(lb.get("Instances", []))},
                           est_month=LIST_PRICE["clb_hour"] * HOURS_MONTH, basis="list", tier="B")
    except (ClientError, BotoCoreError) as e:
        report.skip("elb.classic", region, e)

    # WAF (regional scope). CloudFront scope is handled in the global check.
    try:
        waf = session.client("wafv2", region_name=region, config=BOTO_CFG)
        acls = waf.list_web_acls(Scope="REGIONAL").get("WebACLs", [])
        for acl in acls:
            attached = []
            for rtype in ("APPLICATION_LOAD_BALANCER", "API_GATEWAY", "APPSYNC", "COGNITO_USER_POOL", "APP_RUNNER_SERVICE"):
                try:
                    attached += waf.list_resources_for_web_acl(WebACLArn=acl["ARN"], ResourceType=rtype).get("ResourceArns", [])
                except ClientError:
                    pass
            detail = waf.get_web_acl(Name=acl["Name"], Scope="REGIONAL", Id=acl["Id"]).get("WebACL", {})
            rules = detail.get("Rules", [])
            bot = any("BotControl" in json.dumps(r.get("Statement", {})) for r in rules)
            allowed = safe(report, "waf.allowed", region, metric_stat, cw, "AWS/WAFV2", "AllowedRequests",
                           {"WebACL": acl["Name"], "Region": region, "Rule": "ALL"}, days, "Sum")
            est = LIST_PRICE["waf_acl_month"] + len(rules) * LIST_PRICE["waf_rule_month"] + (LIST_PRICE["waf_bot_control_month"] if bot else 0)
            row = {"name": acl["Name"], "rules": len(rules), "bot_control": bot, "attached_resources": len(attached), "allowed_requests_window": allowed}
            if not attached:
                report.add("waf.unattached", "network", region, f"web ACL {acl['Name']}",
                           f"WAF web ACL attached to nothing ({len(rules)} rules{', Bot Control subscription' if bot else ''}). "
                           "A WAF outlives the load balancer it protected: deleting the ALB does not delete the ACL.",
                           row, est_month=est, basis="list", tier="A",
                           do=f"aws wafv2 get-web-acl --region {region} --scope REGIONAL --name {acl['Name']} --id {acl['Id']} > backup.json ; then delete-web-acl with the lock token",
                           undo="aws wafv2 create-web-acl from the saved JSON")
            elif allowed == 0:
                report.add("waf.zero_traffic", "network", region, f"web ACL {acl['Name']}",
                           f"WAF web ACL attached to {len(attached)} resource(s) but zero allowed requests in {days} days.",
                           row, est_month=est, basis="list", tier="B")
    except (ClientError, BotoCoreError) as e:
        report.skip("wafv2.regional", region, e)

    # Transfer Family: the fee is per protocol per hour, 24/7; bytes are almost free.
    try:
        tr = session.client("transfer", region_name=region, config=BOTO_CFG)
        servers = tr.list_servers().get("Servers", [])
        for s in servers:
            d = tr.describe_server(ServerId=s["ServerId"]).get("Server", {})
            protos = d.get("Protocols", [])
            bytes_in = safe(report, "transfer.bytes", region, metric_stat, cw, "AWS/Transfer", "BytesIn",
                            {"ServerId": s["ServerId"]}, 90, "Sum")
            bytes_out = safe(report, "transfer.bytes", region, metric_stat, cw, "AWS/Transfer", "BytesOut",
                             {"ServerId": s["ServerId"]}, 90, "Sum")
            est = len(protos) * LIST_PRICE["transfer_protocol_hour"] * HOURS_MONTH
            row = {"server": s["ServerId"], "state": d.get("State"), "protocols": protos, "endpoint": d.get("EndpointType"),
                   "bytes_in_90d": bytes_in, "bytes_out_90d": bytes_out}
            if d.get("State") == "ONLINE" and not (bytes_in or bytes_out):
                report.add("transfer.idle_server", "transfer", region, s["ServerId"],
                           f"Transfer Family server ONLINE with {len(protos)} protocol(s) and zero bytes in 90 days: ~$219/month per protocol for nothing.",
                           row, est_month=est, basis="list", tier="B",
                           do=f"Document users/IdP config, then aws transfer stop-server --server-id {s['ServerId']} (billing stops only on delete-server)",
                           undo="Recreate the server (endpoint hostname changes unless a custom hostname was used)")
            elif d.get("State") == "ONLINE" and len(protos) > 1:
                report.add("transfer.extra_protocols", "transfer", region, s["ServerId"],
                           f"{len(protos)} protocols enabled ({', '.join(protos)}): each one costs ~$219/month around the clock. Disable the ones no client uses.",
                           row, est_month=(len(protos) - 1) * LIST_PRICE["transfer_protocol_hour"] * HOURS_MONTH, basis="list", tier="B")
            else:
                report.add("transfer.server", "transfer", region, s["ServerId"],
                           f"Transfer Family server {d.get('State')}: fixed fee ~${est:.0f}/month; 97% of the bill is the fee, not the bytes. "
                           "Alternative: an SFTP on a small instance if you already pay for an unused EC2 Savings Plan.",
                           row, est_month=est, basis="list", tier="info")
    except (ClientError, BotoCoreError) as e:
        report.skip("transfer", region, e)


def check_ec2_ebs(session, report: Report, region: str, days: int) -> None:
    ec2 = session.client("ec2", region_name=region, config=BOTO_CFG)
    cw = session.client("cloudwatch", region_name=region, config=BOTO_CFG)
    eip_hour, eb = price(report, "PublicIPv4", LIST_PRICE["eip_hour"])

    instances = []
    token = None
    while True:
        kw = {"MaxResults": 500}
        if token:
            kw["NextToken"] = token
        r = ec2.describe_instances(**kw)
        for res in r.get("Reservations", []):
            instances.extend(res.get("Instances", []))
        token = r.get("NextToken")
        if not token:
            break
    vols = {}
    for v in _paginate(ec2, "describe_volumes", "Volumes"):
        vols[v["VolumeId"]] = v
    used_amis = {i.get("ImageId") for i in instances}

    # Reserved count per instance type in this region: an instance is "covered" only if the reservations of its
    # type are enough for every running instance of that type (one RI does not cover two t2.micro).
    ri_count: Dict[str, int] = defaultdict(int)
    for c in report.inventory.get("global", {}).get("commitments_calendar", []):
        if c.get("kind") == "EC2-RI" and c.get("region") == region:
            ri_count[c.get("type")] += int(c.get("count") or 1)
    running_by_type: Dict[str, int] = defaultdict(int)
    for i in instances:
        if i.get("State", {}).get("Name") == "running":
            running_by_type[i.get("InstanceType")] += 1
    running, stopped = [], []
    for i in instances:
        name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
        state = i.get("State", {}).get("Name")
        vol_ids = [b.get("Ebs", {}).get("VolumeId") for b in i.get("BlockDeviceMappings", []) if b.get("Ebs")]
        vol_gb = sum(vols.get(v, {}).get("Size", 0) for v in vol_ids)
        vol_cost = sum(vols.get(v, {}).get("Size", 0) * LIST_PRICE["ebs_gb_month"].get(vols.get(v, {}).get("VolumeType", "gp2"), 0.10) for v in vol_ids)
        row = {"id": i["InstanceId"], "name": name, "type": i.get("InstanceType"), "state": state, "launch": str(i.get("LaunchTime")),
               "public_ip": i.get("PublicIpAddress"), "ebs_gb": vol_gb, "ebs_usd_month": round(vol_cost, 2)}
        if state == "running":
            avg = safe(report, "ec2.cpu", region, metric_stat, cw, "AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, days, "Average")
            mx = safe(report, "ec2.cpu", region, metric_stat, cw, "AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, days, "Maximum")
            row.update({"cpu_avg_pct": round(avg, 2) if avg is not None else None, "cpu_max_pct": round(mx, 2) if mx is not None else None})
            running.append(row)
            if avg is not None and mx is not None and avg < 5 and mx < 40:
                on_ri = ri_count.get(i.get("InstanceType"), 0) >= running_by_type.get(i.get("InstanceType"), 0)
                report.add("ec2.underused", "compute", region, f"{i['InstanceId']} {name} ({i.get('InstanceType')})",
                           f"CPU avg {avg:.1f}% / max {mx:.1f}% over {days} days. Candidate for a smaller type or a schedule. "
                           "Check memory and p99 first (a bursty box with high max is NOT a candidate); check tags for grants/contracts."
                           + (" An active EC2 reservation covers this instance type: resizing or stopping it refunds nothing until the RI ends; decide at renewal." if on_ri else ""),
                           row, tier="X" if on_ri else "B",
                           do=f"aws ec2 stop-instances --instance-ids {i['InstanceId']} ; aws ec2 modify-instance-attribute --instance-id {i['InstanceId']} --instance-type <smaller> ; aws ec2 start-instances --instance-ids {i['InstanceId']}",
                           undo="Same sequence with the original type")
            if not name:
                report.add("ec2.unnamed", "compute", region, i["InstanceId"],
                           "Running instance with no Name tag: nobody knows what it is. Not a cut, an investigation (snapshot, stop for 7 days, then decide).",
                           row, tier="C")
        elif state == "stopped":
            stopped.append(row)
            vcpu_hint = i.get("CpuOptions", {}).get("CoreCount", 0) * i.get("CpuOptions", {}).get("ThreadsPerCore", 1)
            report.add("ec2.stopped_with_ebs", "compute", region, f"{i['InstanceId']} {name} ({i.get('InstanceType')})",
                       f"Stopped instance still paying {vol_gb} GB of EBS" + (" and a public IPv4" if i.get("PublicIpAddress") else "") +
                       f". Stopped since: check StateTransitionReason ({(i.get('StateTransitionReason') or '')[:60]}).",
                       row, est_month=vol_cost + (eip_hour * HOURS_MONTH if i.get("PublicIpAddress") else 0), basis="list", tier="B",
                       do=f"Snapshot the volumes, then aws ec2 terminate-instances --instance-ids {i['InstanceId']}",
                       undo="Recreate from the snapshot/AMI by hand")
            if vcpu_hint >= 32:
                report.add("ec2.giant_stopped", "compute", region, f"{i['InstanceId']} {name} ({i.get('InstanceType')})",
                           f"Stopped instance with {vcpu_hint} vCPUs: costs only its disk today, but if someone starts it by mistake it is thousands of dollars a month. "
                           "Put an EventBridge rule on EC2 Instance State-change for this instance id (free), or terminate it after snapshotting.",
                           row, tier="A")
    report.inv(region, "ec2", {"running": running, "stopped": stopped})

    # Detached volumes and gp2 -> gp3.
    detached = [v for v in vols.values() if v.get("State") == "available"]
    for v in detached:
        name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), "")
        est = v["Size"] * LIST_PRICE["ebs_gb_month"].get(v.get("VolumeType", "gp2"), 0.10)
        report.add("ebs.detached", "storage", region, f"{v['VolumeId']} {name} {v['Size']} GB {v.get('VolumeType')}",
                   f"EBS volume attached to nothing since at least its last detach (created {str(v.get('CreateTime'))[:10]}, {age_days(v.get('CreateTime'))} days ago). "
                   "Deletion is irreversible: snapshot first (snapshot costs ~half), or read it from a temporary instance if nobody knows what it holds.",
                   {"volume": v["VolumeId"], "size_gb": v["Size"], "type": v.get("VolumeType"), "created": str(v.get("CreateTime"))},
                   est_month=est, basis="list", tier="B",
                   do=f"aws ec2 create-snapshot --region {region} --volume-id {v['VolumeId']} --description 'pre-delete' ; aws ec2 delete-volume --region {region} --volume-id {v['VolumeId']}",
                   undo="aws ec2 create-volume --snapshot-id <snap>")
    gp2 = [v for v in vols.values() if v.get("VolumeType") == "gp2"]
    if gp2:
        total = sum(v["Size"] for v in gp2)
        report.add("ebs.gp2_to_gp3", "storage", region, f"{len(gp2)} gp2 volumes, {total} GB",
                   "gp2 -> gp3 is ~20% cheaper at the same size, done hot with modify-volume, nothing stops. gp3 includes 3,000 IOPS baseline. "
                   "Only caveat: volumes > 1 TB on gp2 had more baseline IOPS than gp3's 3,000; provision IOPS on those.",
                   {"volumes": [v["VolumeId"] for v in gp2][:50], "gb": total},
                   est_month=total * (LIST_PRICE["ebs_gb_month"]["gp2"] - LIST_PRICE["ebs_gb_month"]["gp3"]), basis="list", tier="A",
                   do=f"for v in <ids>; do aws ec2 modify-volume --region {region} --volume-id $v --volume-type gp3; done",
                   undo="modify-volume back to gp2 (one modification per volume every 6 hours)")

    # Snapshots and AMIs.
    try:
        amis = ec2.describe_images(Owners=["self"]).get("Images", [])
        ami_snaps = set()
        for a in amis:
            for b in a.get("BlockDeviceMappings", []):
                if b.get("Ebs", {}).get("SnapshotId"):
                    ami_snaps.add(b["Ebs"]["SnapshotId"])
        old_amis = [a for a in amis if a.get("ImageId") not in used_amis and (age_days(a.get("CreationDate")) or 0) > 365]
        for a in old_amis:
            size = sum(b.get("Ebs", {}).get("VolumeSize", 0) for b in a.get("BlockDeviceMappings", []))
            report.add("ami.old_unused", "storage", region, f"{a['ImageId']} {a.get('Name', '')[:40]}",
                       f"AMI from {a.get('CreationDate', '')[:10]} not used by any instance. Its snapshots keep billing. "
                       "Irreversible: ask whoever made it. Names hinting at incidents/forensics ('post-hacking', 'before-migration') must NOT be deleted without a decision.",
                       {"ami": a["ImageId"], "name": a.get("Name"), "created": a.get("CreationDate"), "nominal_gb": size},
                       est_month=size * LIST_PRICE["ebs_snapshot_gb_month"], basis="list", tier="B",
                       do=f"aws ec2 deregister-image --region {region} --image-id {a['ImageId']} ; then delete-snapshot on its snapshots",
                       undo="None")
        snaps = list(_paginate(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]))
        old_snaps = [s for s in snaps if s["SnapshotId"] not in ami_snaps and (age_days(s.get("StartTime")) or 0) > 365]
        total_old_gb = sum(s.get("VolumeSize", 0) for s in old_snaps)
        report.inv(region, "ebs_snapshots", {"total": len(snaps), "older_than_1y_not_in_ami": len(old_snaps), "nominal_gb_old": total_old_gb,
                                             "total_nominal_gb": sum(s.get("VolumeSize", 0) for s in snaps)})
        if old_snaps:
            sample = sorted(old_snaps, key=lambda s: s.get("StartTime"))[:15]
            report.add("snapshot.old_manual", "storage", region, f"{len(old_snaps)} snapshots older than 1 year ({total_old_gb} GB nominal)",
                       "Manual EBS snapshots older than a year, not referenced by an AMI. Billed on unique blocks (nominal size is an upper bound: "
                       "`aws ebs list-snapshot-blocks` gives the real one). Deleting is irreversible; archive tier is 75% cheaper with a 90-day minimum.",
                       {"oldest": [{"id": s["SnapshotId"], "gb": s.get("VolumeSize"), "date": str(s.get("StartTime"))[:10],
                                    "desc": (s.get("Description") or "")[:60]} for s in sample]},
                       est_month=total_old_gb * LIST_PRICE["ebs_snapshot_gb_month"] * 0.6, basis="list", tier="B",
                       do=f"aws ec2 delete-snapshot --region {region} --snapshot-id <id>  (or modify-snapshot-tier --storage-tier archive)",
                       undo="None (archive tier: restore takes 24-72 h)")
        # No backup automation at all is the opposite finding, but worth one line.
        try:
            dlm = session.client("dlm", region_name=region, config=BOTO_CFG)
            n_dlm = len(dlm.get_lifecycle_policies().get("Policies", []))
        except (ClientError, BotoCoreError):
            n_dlm = None
        try:
            bk = session.client("backup", region_name=region, config=BOTO_CFG)
            n_bk = len(bk.list_backup_plans().get("BackupPlansList", []))
        except (ClientError, BotoCoreError):
            n_bk = None
        report.inv(region, "backup_automation", {"dlm_policies": n_dlm, "backup_plans": n_bk})
        if running and n_dlm == 0 and n_bk == 0:
            report.add("backup.none", "compute", region, f"{len(running)} running instances",
                       "No DLM policy and no AWS Backup plan in this region: the EC2 instances have no automated backups. Not a cut; a risk.",
                       tier="info")
    except (ClientError, BotoCoreError) as e:
        report.skip("ec2.snapshots", region, e)


def _paginate(client, op: str, key: str, **kw):
    try:
        pag = client.get_paginator(op)
        for page in pag.paginate(**kw):
            for item in page.get(key, []):
                yield item
    except Exception:  # noqa: BLE001 - not every op has a paginator
        r = getattr(client, op)(**kw)
        for item in r.get(key, []):
            yield item


def check_lambda(session, report: Report, region: str, days: int, deep: bool, workers: int) -> None:
    lam = session.client("lambda", region_name=region, config=BOTO_CFG)
    cw = session.client("cloudwatch", region_name=region, config=BOTO_CFG)
    fns = list(_paginate(lam, "list_functions", "Functions"))
    if not fns:
        report.inv(region, "lambda", {"functions": 0})
        return
    period = days * 86400
    start = NOW - dt.timedelta(days=days)

    # Invocations / errors / duration for every function in a handful of GetMetricData calls.
    queries, idmap = [], {}
    for n, f in enumerate(fns):
        name = f["FunctionName"]
        for metric, stat, tag in (("Invocations", "Sum", "inv"), ("Errors", "Sum", "err"), ("Duration", "Average", "dur"), ("Duration", "Maximum", "durmax")):
            qid = f"{tag}_{n}"
            idmap[qid] = (name, tag)
            queries.append(q(qid, "AWS/Lambda", metric, {"FunctionName": name}, period, stat))
    data = safe(report, "lambda.metrics", region, metric_data_batch, cw, queries, start, NOW) or {}
    stats: Dict[str, Dict[str, float]] = defaultdict(dict)
    for qid, vals in data.items():
        name, tag = idmap[qid]
        if vals:
            stats[name][tag] = max(vals) if tag == "durmax" else (sum(vals) if tag in ("inv", "err") else sum(vals) / len(vals))

    unused, deprecated, broken, timeouts, x86 = [], [], [], [], 0
    gbs_by_fn = {}
    for f in fns:
        name = f["FunctionName"]
        s = stats.get(name, {})
        inv = s.get("inv", 0.0)
        mem = f.get("MemorySize", 128)
        gbs_by_fn[name] = inv * (s.get("dur", 0.0) / 1000.0) * (mem / 1024.0)
        if inv == 0:
            unused.append(name)
        if f.get("Runtime") in DEPRECATED_RUNTIMES:
            deprecated.append((name, f.get("Runtime")))
        if inv >= 10 and s.get("err", 0.0) >= 0.95 * inv:
            broken.append((name, int(inv), int(s.get("err", 0))))
        timeout_ms = f.get("Timeout", 3) * 1000
        if inv >= 5 and s.get("durmax", 0) >= 0.98 * timeout_ms and s.get("dur", 0) >= 0.7 * timeout_ms:
            timeouts.append((name, int(inv), int(s.get("dur", 0)), timeout_ms, mem))
        if "arm64" not in (f.get("Architectures") or ["x86_64"]):
            x86 += 1

    top_cost = sorted(gbs_by_fn.items(), key=lambda x: -x[1])[:20]
    report.inv(region, "lambda", {
        "functions": len(fns), "invoked_in_window": len(fns) - len(unused), "zero_invocations": len(unused),
        "deprecated_runtimes": len(deprecated), "x86_64": x86, "arm64": len(fns) - x86,
        "top_gb_seconds_window": [{"function": n, "gb_s": int(v), "usd_window": round(v * LIST_PRICE["lambda_gb_s"], 2)} for n, v in top_cost],
        "zero_invocation_sample": unused[:100], "deprecated_sample": deprecated[:50],
    })
    if broken:
        for name, inv, err in broken:
            report.add("lambda.always_failing", "lambda", region, name,
                       f"{err} errors on {inv} invocations in {days} days: paying to fail. Fix it or disable its trigger.",
                       {"invocations": inv, "errors": err}, tier="A")
    if timeouts:
        for name, inv, dur, tmo, mem in timeouts:
            report.add("lambda.timing_out", "lambda", region, name,
                       f"Average duration {dur / 1000:.0f}s against a {tmo / 1000:.0f}s timeout, max hits the ceiling: it pays full price and delivers nothing, "
                       "unless it is a time-boxed batch that resumes where it stopped (check the code and the Errors metric). "
                       f"At {mem} MB it has ~{mem / 1769:.1f} vCPU; more memory often means SAME cost and a function that works.",
                       {"invocations": inv, "avg_ms": dur, "timeout_ms": tmo, "memory_mb": mem}, tier="A",
                       do=f"aws lambda update-function-configuration --function-name {name} --memory-size <2-4x>",
                       undo="Same command with the old value")
    if len(unused) > 0:
        report.add("lambda.unused_functions", "lambda", region, f"{len(unused)} of {len(fns)} functions",
                   f"Never invoked in {days} days. They cost nothing at rest, but abandoned stacks carry secrets, public URLs, deprecated runtimes and log groups. Hygiene, not savings.",
                   {"sample": unused[:30]}, tier="info")
    if deprecated:
        report.add("lambda.deprecated_runtimes", "lambda", region, f"{len(deprecated)} functions",
                   "Runtimes deprecated per the AWS schedule (verify the current list). Not a cost item: a risk and a future blocked deploy.",
                   {"sample": deprecated[:20]}, tier="info")
    if x86 and len(fns) >= 20:
        top_gbs = sum(v for _, v in top_cost)
        report.add("lambda.arm64", "lambda", region, f"{x86} x86_64 functions",
                   "arm64 (Graviton) is ~20% cheaper per GB-second at equal duration. Worth it only on the top spenders and only if their binary dependencies "
                   "(GDAL, numpy wheels, native modules) are available for arm64.",
                   {"top20_gb_s_window": int(top_gbs)}, est_month=top_gbs * LIST_PRICE["lambda_gb_s"] * 0.2 / days * 30, basis="list", tier="C")

    # SnapStart: cache billed per published version x memory. Versions pile up under most deploy tools.
    snap_fns = [f for f in fns if (f.get("SnapStart") or {}).get("ApplyOn") == "PublishedVersions"]
    ss_rows, total_gb = [], 0.0
    ss_price = LIST_PRICE["lambda_snapstart_gb_s"]
    for f in snap_fns:
        name = f["FunctionName"]
        versions = [v for v in _paginate(lam, "list_versions_by_function", "Versions", FunctionName=name) if v.get("Version") != "$LATEST"]
        ss_versions = [v for v in versions if (v.get("SnapStart") or {}).get("ApplyOn") == "PublishedVersions"]
        aliases = safe(report, "lambda.aliases", region, lambda: lam.list_aliases(FunctionName=name).get("Aliases", [])) or []
        alias_targets = {a.get("FunctionVersion") for a in aliases}
        gb_cached = sum((v.get("MemorySize", f.get("MemorySize", 128)) / 1024.0) for v in ss_versions)
        total_gb += gb_cached
        trig = "unknown"
        try:
            pol = json.loads(lam.get_policy(FunctionName=name).get("Policy", "{}"))
            principals = {str(s.get("Principal", {}).get("Service", "")) for s in pol.get("Statement", [])}
            if any("apigateway" in p or "elasticloadbalancing" in p for p in principals):
                trig = "api"
            elif any("events" in p or "scheduler" in p for p in principals):
                trig = "schedule"
            elif principals:
                trig = ",".join(sorted(p.split(".")[0] for p in principals if p))
        except ClientError:
            trig = "none-in-policy"
        row = {"function": name, "memory_mb": f.get("MemorySize"), "published_versions": len(versions),
               "snapstart_versions": len(ss_versions), "gb_cached": round(gb_cached, 2), "alias_targets": sorted(alias_targets),
               "trigger": trig, "invocations_window": int(stats.get(name, {}).get("inv", 0)),
               "est_usd_month": round(gb_cached * ss_price * 86400 * 30, 2)}
        ss_rows.append(row)
        if len(ss_versions) > 2:
            keep = 2
            report.add("lambda.snapstart_versions", "lambda", region, name,
                       f"{len(ss_versions)} SnapStart versions cached ({gb_cached:.1f} GB): each published version pays the cache 24/7. Keep live + one rollback. "
                       "Serverless prune plugin note: versions pointed at by an alias are preserved IN ADDITION to `number`, so number:1 = 2 versions.",
                       row, est_month=(gb_cached - keep * (f.get('MemorySize', 128) / 1024.0)) * ss_price * 86400 * 30, basis="list", tier="A",
                       do=f"Verify no alias/event-source points at the old version, then aws lambda delete-function --function-name {name} --qualifier <old version>",
                       undo="Redeploy (the code is in git)")
        is_cron = trig == "schedule" or (trig in ("unknown", "none-in-policy") and CRON_NAME_HINT.search(name))
        if is_cron:
            report.add("lambda.snapstart_on_cron", "lambda", region, name,
                       f"SnapStart on a scheduled/worker function (trigger: {trig}, {int(stats.get(name, {}).get('inv', 0))} invocations in {days} days): "
                       "nobody sees a cron's cold start. Remove SnapStart there and keep it for user-facing APIs. Check the timeout margin first.",
                       row, est_month=gb_cached * ss_price * 86400 * 30, basis="list", tier="A",
                       do="Set snapStart: false (or remove) for this function in the deploy config and redeploy",
                       undo="Re-enable and redeploy")
    if ss_rows:
        report.inv(region, "lambda_snapstart", {"functions": len(ss_rows), "gb_cached": round(total_gb, 2),
                                                "est_usd_month": round(total_gb * ss_price * 86400 * 30, 2), "rows": ss_rows})

    # Provisioned concurrency: warm capacity paid 24/7; zero utilization for weeks = pure waste.
    if deep:
        def pc_for(f):
            name = f["FunctionName"]
            try:
                return name, lam.list_provisioned_concurrency_configs(FunctionName=name).get("ProvisionedConcurrencyConfigs", [])
            except ClientError:
                return name, []
        pc_rows = []
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for name, cfgs in ex.map(pc_for, fns):
                for c in cfgs:
                    qual = c.get("FunctionArn", "").split(":")[-1]
                    util = safe(report, "lambda.pc_util", region, metric_stat, cw, "AWS/Lambda", "ProvisionedConcurrencyUtilization",
                                {"FunctionName": name, "Resource": f"{name}:{qual}"}, 90, "Maximum")
                    mem = next((x.get("MemorySize") for x in fns if x["FunctionName"] == name), 128)
                    n = c.get("RequestedProvisionedConcurrentExecutions", 0)
                    est = n * (mem / 1024.0) * LIST_PRICE["lambda_provisioned_gb_s"] * 86400 * 30
                    row = {"function": name, "qualifier": qual, "requested": n, "status": c.get("Status"), "util_max_90d": util,
                           "invocations_window": int(stats.get(name, {}).get("inv", 0)), "est_usd_month": round(est, 2)}
                    pc_rows.append(row)
                    if (util or 0) == 0:
                        report.add("lambda.provisioned_unused", "lambda", region, f"{name}:{qual}",
                                   f"Provisioned concurrency {n} kept warm 24/7 with ZERO utilization in 90 days. The function keeps working without it; it only loses the warm start.",
                                   row, est_month=est, basis="list", tier="A",
                                   do=f"aws lambda delete-provisioned-concurrency-config --function-name {name} --qualifier {qual}",
                                   undo=f"aws lambda put-provisioned-concurrency-config --function-name {name} --qualifier {qual} --provisioned-concurrent-executions {n}")
        if pc_rows:
            report.inv(region, "lambda_provisioned_concurrency", pc_rows)

    # Log groups of functions that no longer exist (worth zero, listed for hygiene) and retention (do NOT cut for money).
    try:
        logs = session.client("logs", region_name=region, config=BOTO_CFG)
        groups = list(_paginate(logs, "describe_log_groups", "logGroups"))
        fn_names = {f["FunctionName"] for f in fns}
        orphan = [g for g in groups if g["logGroupName"].startswith("/aws/lambda/") and g["logGroupName"][12:] not in fn_names]
        no_ret = [g for g in groups if not g.get("retentionInDays")]
        report.inv(region, "log_groups", {"total": len(groups), "stored_gb": gb(sum(g.get("storedBytes", 0) for g in groups)),
                                          "no_retention": len(no_ret), "no_retention_gb": gb(sum(g.get("storedBytes", 0) for g in no_ret)),
                                          "orphan_lambda_groups": len(orphan), "orphan_gb": gb(sum(g.get("storedBytes", 0) for g in orphan))})
        big = sorted(groups, key=lambda g: -g.get("storedBytes", 0))[:10]
        report.add("logs.storage_is_not_the_cost", "cloudwatch", region, f"{len(groups)} log groups, {gb(sum(g.get('storedBytes', 0) for g in groups))} GB stored",
                   f"{len(no_ret)} groups without retention. Log STORAGE costs $0.03/GB-month: setting retention 'to save money' destroys evidence for pennies. "
                   "CloudWatch money is in ingestion ($0.50/GB), GetMetricData, custom metrics, Database/Container Insights. Cut what you WRITE, not what you keep.",
                   {"largest": [{"group": g["logGroupName"], "gb": gb(g.get("storedBytes", 0)), "retention": g.get("retentionInDays")} for g in big]},
                   est_month=gb(sum(g.get("storedBytes", 0) for g in groups)) * 0.03, basis="list", tier="X")
    except (ClientError, BotoCoreError) as e:
        report.skip("logs.describe", region, e)


def check_rds(session, report: Report, region: str, days: int) -> None:
    rds = session.client("rds", region_name=region, config=BOTO_CFG)
    cw = session.client("cloudwatch", region_name=region, config=BOTO_CFG)
    eip_hour, eb = price(report, "PublicIPv4", LIST_PRICE["eip_hour"])
    instances = list(_paginate(rds, "describe_db_instances", "DBInstances"))
    clusters = list(_paginate(rds, "describe_db_clusters", "DBClusters"))
    live_ids = {i["DBInstanceIdentifier"] for i in instances}
    live_clusters = {c["DBClusterIdentifier"] for c in clusters}
    rows = []
    for i in instances:
        iid = i["DBInstanceIdentifier"]
        conn_avg = safe(report, "rds.conn", region, metric_stat, cw, "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": iid}, days, "Average")
        conn_max = safe(report, "rds.conn", region, metric_stat, cw, "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": iid}, days, "Maximum")
        cpu = safe(report, "rds.cpu", region, metric_stat, cw, "AWS/RDS", "CPUUtilization", {"DBInstanceIdentifier": iid}, days, "Average")
        cpu_max = safe(report, "rds.cpu", region, metric_stat, cw, "AWS/RDS", "CPUUtilization", {"DBInstanceIdentifier": iid}, days, "Maximum")
        row = {"id": iid, "class": i.get("DBInstanceClass"), "engine": i.get("Engine"), "cluster": i.get("DBClusterIdentifier"),
               "public": i.get("PubliclyAccessible"), "multi_az": i.get("MultiAZ"), "storage_type": i.get("StorageType"),
               "allocated_gb": i.get("AllocatedStorage"), "backup_retention_days": i.get("BackupRetentionPeriod"),
               "db_insights": i.get("DatabaseInsightsMode"), "perf_insights_retention": i.get("PerformanceInsightsRetentionPeriod"),
               "conn_avg": conn_avg, "conn_max": conn_max, "cpu_avg": cpu, "cpu_max": cpu_max}
        rows.append(row)
        if i.get("PubliclyAccessible"):
            report.add("rds.public_endpoint", "rds", region, iid,
                       "Publicly accessible database: one billed IPv4 and an exposure. Making it private breaks anyone connecting from a laptop by allowlisted IP: decide with the people who use it.",
                       row, est_month=eip_hour * HOURS_MONTH, basis=eb, tier="B")
        if conn_max is not None and conn_max == 0 and i.get("DBInstanceClass") != "db.serverless":
            report.add("rds.no_connections", "rds", region, f"{iid} ({i.get('DBInstanceClass')})",
                       f"Zero connections in {days} days on a provisioned instance. Stop it (7 days max on RDS, then it restarts), convert to Serverless v2 min 0, or snapshot and delete.",
                       row, tier="B",
                       do=f"aws rds modify-db-instance --db-instance-identifier {iid} --db-instance-class db.serverless  (Aurora) / aws rds stop-db-instance (RDS)",
                       undo="modify back / start-db-instance")
        elif conn_avg is not None and conn_avg < 1 and cpu is not None and cpu < 10 and i.get("DBInstanceClass") not in ("db.serverless",):
            report.add("rds.nearly_idle", "rds", region, f"{iid} ({i.get('DBInstanceClass')})",
                       f"Avg {conn_avg:.2f} connections, CPU {cpu:.1f}% over {days} days. Serverless v2 with min 0 ACU fits this shape; check nothing keeps a connection open (it would never sleep).",
                       row, tier="B")
        if i.get("DatabaseInsightsMode") == "advanced" and (conn_avg or 0) < 5:
            vcpu_guess = {"large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "medium": 2, "small": 2, "micro": 2}.get((i.get("DBInstanceClass") or "").split(".")[-1], 2)
            report.add("rds.insights_advanced", "rds", region, iid,
                       "Database Insights 'advanced' billed per vCPU-hour on a database with almost no connections. Standard mode is free. History already collected is lost on downgrade: export first if someone is investigating.",
                       row, est_month=vcpu_guess * LIST_PRICE["rds_db_insights_advanced_vcpu_hour"] * HOURS_MONTH, basis="list", tier="B",
                       do=f"aws rds modify-db-instance --db-instance-identifier {iid} --database-insights-mode standard --performance-insights-retention-period 7 --apply-immediately",
                       undo="Same with advanced / 465 (history does not come back)")
        if (i.get("BackupRetentionPeriod") or 0) < 7 and i.get("Engine") and "aurora" in i.get("Engine", "") and not i.get("ReadReplicaSourceDBInstanceIdentifier"):
            report.add("rds.short_backup_retention", "rds", region, iid,
                       f"Backup retention {i.get('BackupRetentionPeriod')} days on a production-looking database. Opposite of a cut: raise it (7-14 days).",
                       row, tier="info")
    report.inv(region, "rds_instances", rows)

    # Aurora clusters: I/O-Optimized break-even and Serverless v2 that never sleeps.
    crow = []
    for c in clusters:
        cid = c["DBClusterIdentifier"]
        rd = safe(report, "aurora.io", region, metric_stat, cw, "AWS/RDS", "VolumeReadIOPs", {"DBClusterIdentifier": cid}, days, "Sum") or 0
        wr = safe(report, "aurora.io", region, metric_stat, cw, "AWS/RDS", "VolumeWriteIOPs", {"DBClusterIdentifier": cid}, days, "Sum") or 0
        vol_bytes = safe(report, "aurora.vol", region, metric_stat, cw, "AWS/RDS", "VolumeBytesUsed", {"DBClusterIdentifier": cid}, days, "Average") or 0
        io_month = (rd + wr) / days * 30
        st = c.get("StorageType") or "aurora"
        s2 = c.get("ServerlessV2ScalingConfiguration") or {}
        members = [m for m in instances if m.get("DBClusterIdentifier") == cid]
        row = {"id": cid, "engine": c.get("Engine"), "storage_type": st, "members": [m["DBInstanceIdentifier"] for m in members],
               "io_per_month": int(io_month), "volume_gb": gb(vol_bytes), "serverless_v2_min_acu": s2.get("MinCapacity"), "serverless_v2_max_acu": s2.get("MaxCapacity"),
               "backup_retention": c.get("BackupRetentionPeriod")}
        crow.append(row)
        # I/O-Optimized: pays ~30% more per instance and 2.25x storage to zero the I/O bill. Break-even is high.
        if st == "aurora-iopt1" and members:
            io_cost_std = io_month / 1e6 * LIST_PRICE["aurora_io_per_million"]
            storage_delta = gb(vol_bytes) * (LIST_PRICE["aurora_storage_gb_month"]["aurora-iopt1"] - LIST_PRICE["aurora_storage_gb_month"]["aurora"])
            report.add("aurora.io_optimized_check", "rds", region, cid,
                       f"I/O-Optimized cluster doing ~{io_month / 1e6:.0f} M I/O per month. Standard would bill that at ~${io_cost_std:.0f}/month, "
                       f"while I/O-Optimized adds ~${storage_delta:.0f}/month of storage premium plus ~30% on every instance. "
                       "Rule of thumb: I/O-Optimized wins only when I/O charges would exceed ~25% of the cluster bill. Switch is online, allowed once per 30 days. "
                       "RIs cover both configurations, but I/O-Optimized consumes 30% more normalized units, so coverage improves on Standard.",
                       row, tier="C",
                       do=f"aws rds modify-db-cluster --db-cluster-identifier {cid} --storage-type aurora --apply-immediately",
                       undo=f"--storage-type aurora-iopt1 (only after 30 days)")
        if s2 and (s2.get("MinCapacity") or 0) == 0:
            acu_avg = safe(report, "aurora.acu", region, metric_stat, cw, "AWS/RDS", "ServerlessDatabaseCapacity", {"DBClusterIdentifier": cid}, days, "Average")
            acu_max = safe(report, "aurora.acu", region, metric_stat, cw, "AWS/RDS", "ServerlessDatabaseCapacity", {"DBClusterIdentifier": cid}, days, "Maximum")
            row["acu_avg"], row["acu_max"] = acu_avg, acu_max
            # The minimum is useless here: it touches 0 for a minute during a scale event. The average tells the story:
            # 0.48 over two weeks on a 0.5 ACU floor means the database sleeps 4% of the time.
            if acu_avg is not None and acu_avg >= 0.3 and (acu_max or 0) <= 2 * max(acu_avg, 0.5):
                report.add("aurora.serverless_never_sleeps", "rds", region, cid,
                           f"Serverless v2 with min 0 ACU averaged {acu_avg:.2f} ACU over {days} days (max {acu_max}): it sits on its 0.5 ACU floor almost all the time. "
                           "Something keeps a connection open (pool, monitor, cron). Find that client and give it a timeout; the DB then scales to zero when idle "
                           "(first query after a pause takes ~15 s).",
                           row, est_month=acu_avg * 0.12 * HOURS_MONTH, basis="list", tier="C")
        elif s2 and (s2.get("MinCapacity") or 0) > 0:
            conn = max((m.get("conn_max") or 0) for m in rows if m["id"] in row["members"]) if row["members"] else None
            if conn == 0:
                report.add("aurora.serverless_min_acu_idle", "rds", region, cid,
                           f"Serverless v2 pinned at min {s2.get('MinCapacity')} ACU with zero connections: set MinCapacity 0 (engine version permitting).",
                           row, est_month=s2.get("MinCapacity", 0.5) * 0.12 * HOURS_MONTH, basis="list", tier="B",
                           do=f"aws rds modify-db-cluster --db-cluster-identifier {cid} --serverless-v2-scaling-configuration MinCapacity=0,MaxCapacity={s2.get('MaxCapacity', 1)} --apply-immediately")
    report.inv(region, "rds_clusters", crow)

    # Manual snapshots: the classic 'backup of a database that no longer exists'.
    try:
        snaps = list(_paginate(rds, "describe_db_snapshots", "DBSnapshots", SnapshotType="manual"))
        csnaps = list(_paginate(rds, "describe_db_cluster_snapshots", "DBClusterSnapshots", SnapshotType="manual"))
        srows = []
        for s in snaps:
            src_alive = s.get("DBInstanceIdentifier") in live_ids
            row = {"id": s["DBSnapshotIdentifier"], "source": s.get("DBInstanceIdentifier"), "source_alive": src_alive,
                   "gb_allocated": s.get("AllocatedStorage"), "created": str(s.get("SnapshotCreateTime"))[:10], "age_days": age_days(s.get("SnapshotCreateTime"))}
            srows.append(row)
        for s in csnaps:
            src_alive = s.get("DBClusterIdentifier") in live_clusters
            srows.append({"id": s["DBClusterSnapshotIdentifier"], "source": s.get("DBClusterIdentifier"), "source_alive": src_alive, "cluster": True,
                          "gb_allocated": s.get("AllocatedStorage"), "created": str(s.get("SnapshotCreateTime"))[:10], "age_days": age_days(s.get("SnapshotCreateTime"))})
        report.inv(region, "rds_manual_snapshots", srows)
        for row in srows:
            if (row.get("age_days") or 0) > 180:
                est = (row.get("gb_allocated") or 0) * 0.095 * 0.7  # billed on used data, allocated is an upper bound
                report.add("rds.old_manual_snapshot", "rds", region, row["id"],
                           f"Manual snapshot from {row['created']} ({row['age_days']} days), source {'still exists' if row['source_alive'] else 'DELETED'}. "
                           "Billed at ~$0.095/GB-month on used data. Irreversible: instead of deleting, export to S3 Glacier Instant Retrieval (~20% of the price) with start-export-task.",
                           row, est_month=est, basis="list", tier="B",
                           do=f"aws rds start-export-task ... (to S3) then aws rds delete-db{'-cluster' if row.get('cluster') else ''}-snapshot --db{'-cluster' if row.get('cluster') else ''}-snapshot-identifier {row['id']}",
                           undo="None")
    except (ClientError, BotoCoreError) as e:
        report.skip("rds.snapshots", region, e)


def check_containers(session, report: Report, region: str, days: int) -> None:
    # ECR: untagged images and repos without a lifecycle policy. Shared layers are billed once (~0.7x of the nominal sum).
    try:
        ecr = session.client("ecr", region_name=region, config=BOTO_CFG)
        repos = list(_paginate(ecr, "describe_repositories", "repositories"))
        rrows = []
        for r in repos:
            name = r["repositoryName"]
            try:
                ecr.get_lifecycle_policy(repositoryName=name)
                has_policy = True
            except ClientError as e:
                has_policy = e.response.get("Error", {}).get("Code") != "LifecyclePolicyNotFoundException"
            imgs = list(_paginate(ecr, "describe_images", "imageDetails", repositoryName=name))
            total = sum(i.get("imageSizeInBytes", 0) for i in imgs)
            untagged = [i for i in imgs if not i.get("imageTags")]
            unt_bytes = sum(i.get("imageSizeInBytes", 0) for i in untagged)
            oldest = min((i.get("imagePushedAt") for i in imgs if i.get("imagePushedAt")), default=None)
            newest = max((i.get("imagePushedAt") for i in imgs if i.get("imagePushedAt")), default=None)
            row = {"repo": name, "images": len(imgs), "gb_nominal": gb(total), "untagged": len(untagged), "untagged_gb": gb(unt_bytes),
                   "lifecycle_policy": has_policy, "oldest_push": str(oldest)[:10] if oldest else None, "newest_push": str(newest)[:10] if newest else None}
            rrows.append(row)
            est_untagged = gb(unt_bytes) * 0.7 * LIST_PRICE["ecr_gb_month"]
            if not has_policy and len(imgs) > 5:
                report.add("ecr.no_lifecycle", "containers", region, name,
                           f"{len(imgs)} images, {gb(total)} GB nominal, {len(untagged)} untagged ({gb(unt_bytes)} GB), no lifecycle policy. "
                           "Preview the policy first (start-lifecycle-policy-preview) and check no task definition pins an untagged image by digest.",
                           row, est_month=est_untagged, basis="list", tier="A",
                           do=f"aws ecr put-lifecycle-policy --repository-name {name} --lifecycle-policy-text '{{\"rules\":[{{\"rulePriority\":1,\"selection\":{{\"tagStatus\":\"untagged\",\"countType\":\"sinceImagePushed\",\"countUnit\":\"days\",\"countNumber\":7}},\"action\":{{\"type\":\"expire\"}}}}]}}'",
                           undo="delete-lifecycle-policy (expired images do not come back)")
            elif has_policy and len(untagged) > 10:
                report.add("ecr.loose_policy", "containers", region, name,
                           f"Lifecycle policy exists but {len(untagged)} untagged images remain ({gb(unt_bytes)} GB): tighten it (keep 5 untagged, not 20).",
                           row, est_month=est_untagged * 0.7, basis="list", tier="B")
            if newest and (age_days(newest) or 0) > 365 and total > 1e9:
                report.add("ecr.stale_repo", "containers", region, name,
                           f"No push in {age_days(newest)} days, {gb(total)} GB. Probably a dead project's images.",
                           row, est_month=gb(total) * 0.7 * LIST_PRICE["ecr_gb_month"], basis="list", tier="B",
                           do=f"aws ecr delete-repository --region {region} --repository-name {name} --force", undo="Rebuild the image")
        report.inv(region, "ecr", {"repos": len(repos), "gb_nominal": round(sum(r["gb_nominal"] for r in rrows), 1), "rows": rrows})
    except (ClientError, BotoCoreError) as e:
        report.skip("ecr", region, e)

    # ECS: Container Insights on idle clusters costs; empty clusters are free.
    try:
        ecs = session.client("ecs", region_name=region, config=BOTO_CFG)
        arns = list(_paginate(ecs, "list_clusters", "clusterArns"))
        rows = []
        for i in range(0, len(arns), 100):
            for c in ecs.describe_clusters(clusters=arns[i:i + 100], include=["SETTINGS", "STATISTICS"]).get("clusters", []):
                ci = next((s.get("value") for s in c.get("settings", []) if s.get("name") == "containerInsights"), "disabled")
                row = {"cluster": c.get("clusterName"), "running_tasks": c.get("runningTasksCount", 0), "services": c.get("activeServicesCount", 0),
                       "instances": c.get("registeredContainerInstancesCount", 0), "container_insights": ci}
                rows.append(row)
                if ci in ("enabled", "enhanced") and c.get("runningTasksCount", 0) == 0:
                    report.add("ecs.insights_on_idle_cluster", "containers", region, c.get("clusterName"),
                               f"Container Insights '{ci}' on a cluster with 0 running tasks: custom metrics billed for nothing.",
                               row, est_month=15.0, basis="list", tier="A",
                               do=f"aws ecs update-cluster-settings --region {region} --cluster {c.get('clusterName')} --settings name=containerInsights,value=disabled",
                               undo=f"same with value={ci}")
        report.inv(region, "ecs", rows)
    except (ClientError, BotoCoreError) as e:
        report.skip("ecs", region, e)

    # App Runner: provisioned memory is billed every hour the service is not paused.
    try:
        ar = session.client("apprunner", region_name=region, config=BOTO_CFG)
        cw = session.client("cloudwatch", region_name=region, config=BOTO_CFG)
        for s in ar.list_services().get("ServiceSummaryList", []):
            d = ar.describe_service(ServiceArn=s["ServiceArn"]).get("Service", {})
            mem_gb = float(str(d.get("InstanceConfiguration", {}).get("Memory", "2 GB")).split()[0]) if d.get("InstanceConfiguration") else 2.0
            if mem_gb > 64:  # value given in MB
                mem_gb = mem_gb / 1024
            reqs = safe(report, "apprunner.requests", region, metric_stat, cw, "AWS/AppRunner", "Requests",
                        {"ServiceName": s.get("ServiceName"), "ServiceID": s.get("ServiceId")}, 30, "Sum")
            est = mem_gb * LIST_PRICE["apprunner_provisioned_gb_hour"] * HOURS_MONTH
            row = {"service": s.get("ServiceName"), "status": s.get("Status"), "memory_gb": mem_gb, "requests_30d": reqs}
            if s.get("Status") == "RUNNING" and (reqs or 0) < 3000:
                report.add("apprunner.low_traffic", "containers", region, s.get("ServiceName"),
                           f"App Runner service RUNNING with {int(reqs or 0)} requests in 30 days: ~${est:.0f}/month of provisioned memory for a handful of hits. Pause it, or move it to Lambda.",
                           row, est_month=est, basis="list", tier="B",
                           do=f"aws apprunner pause-service --service-arn <arn>", undo="aws apprunner resume-service --service-arn <arn>")
    except (ClientError, BotoCoreError) as e:
        report.skip("apprunner", region, e)

    # SageMaker: stopped notebooks cost zero (do not count them), running ones and endpoints do.
    try:
        sm = session.client("sagemaker", region_name=region, config=BOTO_CFG)
        nbs = sm.list_notebook_instances(MaxResults=100).get("NotebookInstances", [])
        eps = sm.list_endpoints(MaxResults=100).get("Endpoints", [])
        report.inv(region, "sagemaker", {"notebooks": len(nbs), "notebooks_in_service": sum(1 for n in nbs if n.get("NotebookInstanceStatus") == "InService"),
                                         "endpoints": len(eps)})
        for n in nbs:
            if n.get("NotebookInstanceStatus") == "InService":
                report.add("sagemaker.notebook_running", "compute", region, f"{n.get('NotebookInstanceName')} ({n.get('InstanceType')})",
                           "SageMaker notebook InService: billed per hour like an EC2 instance, usually forgotten open. Stopped notebooks cost nothing.",
                           tier="A", do=f"aws sagemaker stop-notebook-instance --notebook-instance-name {n.get('NotebookInstanceName')}")
        for e in eps:
            if e.get("EndpointStatus") == "InService":
                report.add("sagemaker.endpoint", "compute", region, e.get("EndpointName"),
                           "SageMaker real-time endpoint InService: billed 24/7. Check its Invocations metric; serverless inference or a scale-to-zero worker may fit.",
                           tier="C")
    except (ClientError, BotoCoreError) as e:
        report.skip("sagemaker", region, e)

    # Lightsail: snapshots that outlived their instances.
    try:
        ls = session.client("lightsail", region_name=region, config=BOTO_CFG)
        n_inst = len(ls.get_instances().get("instances", []))
        snaps = ls.get_instance_snapshots().get("instanceSnapshots", [])
        dsnaps = ls.get_disk_snapshots().get("diskSnapshots", [])
        total_gb = sum(s.get("sizeInGb", 0) for s in snaps) + sum(s.get("sizeInGb", 0) for s in dsnaps)
        if (snaps or dsnaps) and n_inst == 0:
            report.add("lightsail.orphan_snapshots", "storage", region, f"{len(snaps) + len(dsnaps)} snapshots, {total_gb} GB, 0 instances",
                       "Lightsail snapshots with no Lightsail instance left: a dead website's last copies. Keep the newest, drop the older ones after asking.",
                       {"snapshots": [{"name": s.get("name"), "gb": s.get("sizeInGb"), "created": str(s.get("createdAt"))[:10]} for s in snaps + dsnaps]},
                       est_month=total_gb * LIST_PRICE["lightsail_snapshot_gb_month"], basis="list", tier="B",
                       do="aws lightsail delete-instance-snapshot --instance-snapshot-name <name>", undo="None")
    except (ClientError, BotoCoreError) as e:
        report.skip("lightsail", region, e)


def check_observability_misc(session, report: Report, region: str, days: int) -> None:
    cw = session.client("cloudwatch", region_name=region, config=BOTO_CFG)
    try:
        alarms = list(_paginate(cw, "describe_alarms", "MetricAlarms", StateValue="ALARM"))
        stuck = [a for a in alarms if (age_days(a.get("StateUpdatedTimestamp")) or 0) > 7]
        if stuck:
            report.add("cw.alarms_stuck", "cloudwatch", region, f"{len(stuck)} alarms in ALARM for > 7 days",
                       "An alarm that is always red is an alarm nobody reads. Either the threshold is wrong for the service's real scale, or the thing it watches is dead. Fix or disable-alarm-actions.",
                       {"alarms": [{"name": a["AlarmName"], "since": str(a.get("StateUpdatedTimestamp"))[:10]} for a in stuck[:20]]}, tier="info")
    except (ClientError, BotoCoreError) as e:
        report.skip("cloudwatch.meta", region, e)

    # API Gateway execution logging at INFO + data trace: expensive ingestion and request bodies in logs.
    try:
        apigw = session.client("apigateway", region_name=region, config=BOTO_CFG)
        apis = list(_paginate(apigw, "get_rest_apis", "items"))
        noisy = []
        for a in apis:
            for st in apigw.get_stages(restApiId=a["id"]).get("item", []):
                ms = st.get("methodSettings", {}).get("*/*", {})
                if ms.get("loggingLevel") == "INFO" or ms.get("dataTraceEnabled"):
                    noisy.append({"api": a.get("name"), "id": a["id"], "stage": st.get("stageName"),
                                  "level": ms.get("loggingLevel"), "data_trace": ms.get("dataTraceEnabled")})
        report.inv(region, "apigateway", {"rest_apis": len(apis), "stages_logging_info_or_trace": len(noisy)})
        if noisy:
            report.add("apigw.verbose_logging", "cloudwatch", region, f"{len(noisy)} stages",
                       "Execution logging at INFO and/or full data trace: every request and response body is written to CloudWatch Logs ($0.50/GB ingested) and secrets end up in logs. Set ERROR and dataTrace=false on production stages.",
                       {"stages": noisy[:20]}, tier="B",
                       do=f"aws apigateway update-stage --region {region} --rest-api-id <id> --stage-name <stage> --patch-operations op=replace,path=/*/*/logging/loglevel,value=ERROR op=replace,path=/*/*/logging/dataTrace,value=false",
                       undo="Same with INFO / true")
    except (ClientError, BotoCoreError) as e:
        report.skip("apigateway", region, e)

    # Secrets Manager and KMS.
    try:
        sm = session.client("secretsmanager", region_name=region, config=BOTO_CFG)
        secrets = list(_paginate(sm, "list_secrets", "SecretList"))
        stale = [s for s in secrets if s.get("LastAccessedDate") is None or (age_days(s.get("LastAccessedDate")) or 0) > 180]
        report.inv(region, "secrets", {"total": len(secrets), "not_accessed_180d": len(stale),
                                       "est_usd_month": round(len(secrets) * LIST_PRICE["secret_month"], 2)})
        if stale:
            report.add("secrets.stale", "misc", region, f"{len(stale)} of {len(secrets)} secrets not read in 180+ days",
                       "$0.40/month each. LastAccessedDate is daily and best-effort: a secret read from a .env copy or by a yearly cron looks dead. Delete with a 30-day recovery window, never force.",
                       {"sample": [s.get("Name") for s in stale[:30]]}, est_month=len(stale) * LIST_PRICE["secret_month"], basis="list", tier="B",
                       do="aws secretsmanager delete-secret --secret-id <name> --recovery-window-in-days 30", undo="aws secretsmanager restore-secret --secret-id <name> (within 30 days)")
    except (ClientError, BotoCoreError) as e:
        report.skip("secretsmanager", region, e)
    try:
        kms = session.client("kms", region_name=region, config=BOTO_CFG)
        disabled = []
        for k in _paginate(kms, "list_keys", "Keys"):
            try:
                m = kms.describe_key(KeyId=k["KeyId"]).get("KeyMetadata", {})
            except ClientError:
                continue
            if m.get("KeyManager") == "CUSTOMER" and m.get("KeyState") == "Disabled":
                disabled.append(m.get("KeyId"))
        if disabled:
            report.add("kms.disabled_keys", "misc", region, f"{len(disabled)} disabled customer keys",
                       "Disabled CMKs still cost $1/month each. Schedule deletion (7-30 day window) only if nothing encrypted with them must ever be read again.",
                       {"keys": disabled}, est_month=len(disabled) * LIST_PRICE["kms_key_month"], basis="list", tier="B")
    except (ClientError, BotoCoreError) as e:
        report.skip("kms", region, e)

    # EventBridge rules firing every few minutes in non-production-looking stacks.
    try:
        ev = session.client("events", region_name=region, config=BOTO_CFG)
        rules = list(_paginate(ev, "list_rules", "Rules"))
        hot = [r for r in rules if r.get("State") == "ENABLED" and r.get("ScheduleExpression") and
               re.search(r"rate\(\s*[1-9]\s+minutes?\)|cron\(\*/[1-9] ", r.get("ScheduleExpression", ""))]
        staging = [r for r in hot if re.search(r"staging|stage|dev|test|sandbox", r.get("Name", ""), re.I)]
        report.inv(region, "eventbridge", {"rules": len(rules), "high_frequency": len(hot), "high_frequency_nonprod": len(staging)})
        if staging:
            report.add("events.hot_nonprod_schedule", "lambda", region, f"{len(staging)} non-prod rules firing every few minutes",
                       "Staging/dev schedules running 24/7 at production cadence: invocations, logs and DB wake-ups for nobody. Disable or slow them down.",
                       {"rules": [{"name": r["Name"], "schedule": r.get("ScheduleExpression")} for r in staging[:20]]}, tier="A",
                       do="aws events disable-rule --name <rule>", undo="aws events enable-rule --name <rule>")
    except (ClientError, BotoCoreError) as e:
        report.skip("events", region, e)


# ---------------------------------------------------------------------------
# S3 (global bucket list, metrics in each bucket's region)
# ---------------------------------------------------------------------------
def bucket_region(s3, name: str) -> str:
    loc = None
    for attempt in range(3):
        try:
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
            break
        except (ClientError, BotoCoreError):
            if attempt == 2:
                return "unknown"
            time.sleep(1 + attempt)
    if loc is None:
        return "us-east-1"
    if loc == "EU":
        return "eu-west-1"
    return loc


def check_s3(session, report: Report, regions: List[str], workers: int) -> None:
    s3 = session.client("s3", config=BOTO_CFG)
    buckets = s3.list_buckets().get("Buckets", [])
    if not buckets:
        return
    log(f"S3: {len(buckets)} buckets")
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        regions_of = dict(zip([b["Name"] for b in buckets], ex.map(lambda b: bucket_region(s3, b["Name"]), buckets)))
    by_region: Dict[str, List[str]] = defaultdict(list)
    for name, reg in regions_of.items():
        by_region[reg].append(name)

    # Sizes and object counts from the free daily CloudWatch metrics, now and ~90 days ago.
    size_now: Dict[str, Dict[str, float]] = defaultdict(dict)
    size_old: Dict[str, Dict[str, float]] = defaultdict(dict)
    objs_now: Dict[str, float] = {}
    objs_old: Dict[str, float] = {}
    start = NOW - dt.timedelta(days=92)
    for reg, names in by_region.items():
        if reg == "unknown" or (regions and reg not in regions and reg not in ("us-east-1",)):
            # still try: metrics live in the bucket's region even if the user limited the scan
            pass
        try:
            cw = session.client("cloudwatch", region_name=reg, config=BOTO_CFG)
        except Exception:  # noqa: BLE001
            continue
        queries, idmap = [], {}
        n = 0
        for b in names:
            for st in S3_STORAGE_TYPES:
                qid = f"s_{n}"
                idmap[qid] = (b, st)
                queries.append(q(qid, "AWS/S3", "BucketSizeBytes", {"BucketName": b, "StorageType": st}, 86400, "Average"))
                n += 1
            qid = f"o_{n}"
            idmap[qid] = (b, "objects")
            queries.append(q(qid, "AWS/S3", "NumberOfObjects", {"BucketName": b, "StorageType": "AllStorageTypes"}, 86400, "Average"))
            n += 1
        data = safe(report, "s3.metrics", reg, metric_data_batch, cw, queries, start, NOW) or {}
        for qid, vals in data.items():
            if not vals:
                continue
            b, st = idmap[qid]
            if st == "objects":
                objs_now[b], objs_old[b] = vals[0], vals[-1]
            else:
                size_now[b][st], size_old[b][st] = vals[0], vals[-1]

    def per_bucket(name: str) -> Dict[str, Any]:
        reg = regions_of[name]
        row: Dict[str, Any] = {"bucket": name, "region": reg}
        tot_now = sum(size_now.get(name, {}).values())
        tot_old = sum(size_old.get(name, {}).values())
        row["gb"] = gb(tot_now)
        row["gb_90d_ago"] = gb(tot_old)
        row["growth_gb_year"] = round((gb(tot_now) - gb(tot_old)) * 4, 1)
        row["objects"] = int(objs_now.get(name, 0))
        row["avg_object_kb"] = round(tot_now / objs_now[name] / 1024, 1) if objs_now.get(name) else None
        row["classes"] = {st: gb(v) for st, v in size_now.get(name, {}).items() if v > 0}
        est = 0.0
        for st, v in size_now.get(name, {}).items():
            est += gb(v) * LIST_PRICE["s3_gb_month"].get(st, 0.023 if "Overhead" not in st else 0.023)
        row["est_usd_month"] = round(est, 2)
        rules = None
        try:
            rules = s3.get_bucket_lifecycle_configuration(Bucket=name).get("Rules", [])
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchLifecycleConfiguration":
                rules = []
        row["lifecycle_rules"] = len(rules) if rules is not None else None
        if rules:
            enabled = [r for r in rules if r.get("Status") == "Enabled"]
            row["has_expiration"] = any("Expiration" in r or "NoncurrentVersionExpiration" in r for r in enabled)
            row["has_multipart_abort"] = any("AbortIncompleteMultipartUpload" in r for r in enabled)
            row["int_days0"] = any(t.get("StorageClass") == "INTELLIGENT_TIERING" and t.get("Days", 1) == 0
                                   for r in enabled for t in r.get("Transitions", []))
            row["rule_prefixes"] = [str((r.get("Filter") or {}).get("Prefix") or r.get("Prefix") or "") for r in enabled][:5]
        else:
            row["has_expiration"] = False
            row["has_multipart_abort"] = False
            row["int_days0"] = False
        try:
            row["versioning"] = s3.get_bucket_versioning(Bucket=name).get("Status")
        except ClientError:
            row["versioning"] = None
        try:
            row["public_policy"] = s3.get_bucket_policy_status(Bucket=name).get("PolicyStatus", {}).get("IsPublic")
        except ClientError:
            row["public_policy"] = False
        try:
            pab = s3.get_public_access_block(Bucket=name).get("PublicAccessBlockConfiguration", {})
            row["block_public_all"] = all(pab.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
        except ClientError:
            row["block_public_all"] = False
        try:
            mp = s3.list_multipart_uploads(Bucket=name, MaxUploads=1000)
            row["open_multipart_uploads"] = len(mp.get("Uploads", []))
        except ClientError:
            row["open_multipart_uploads"] = None
        return row

    def per_bucket_safe(name: str) -> Optional[Dict[str, Any]]:
        for attempt in range(3):
            try:
                return per_bucket(name)
            except (ClientError, BotoCoreError) as e:
                if attempt == 2:
                    report.skip(f"s3.bucket[{name}]", regions_of.get(name, "?"), e)
                time.sleep(1 + attempt)
        return None

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        rows = [r for r in ex.map(per_bucket_safe, [b["Name"] for b in buckets]) if r]
    rows.sort(key=lambda r: -r["gb"])
    report.inv("global", "s3", {"buckets": len(rows), "total_gb": round(sum(r["gb"] for r in rows), 1),
                                "est_usd_month": round(sum(r["est_usd_month"] for r in rows), 2),
                                "no_lifecycle": sum(1 for r in rows if not r.get("lifecycle_rules")),
                                "no_multipart_abort": sum(1 for r in rows if not r.get("has_multipart_abort")),
                                "public_policy": sum(1 for r in rows if r.get("public_policy")),
                                "rows": rows})

    for r in rows:
        name = r["bucket"]
        reg = r["region"]
        if r["gb"] >= 50 and not r.get("has_expiration"):
            report.add("s3.no_expiration_growing" if r["growth_gb_year"] > 100 else "s3.no_expiration", "s3", reg,
                       f"{name} ({r['gb']} GB, {r['objects']:,} objects)",
                       f"No expiration rule; grew {r['growth_gb_year']:+.0f} GB/year (~${r['growth_gb_year'] * 0.023:.0f}/year of NEW spend every year). "
                       "Whether raw device data can expire is a product decision (derived tiles: yes; raw sensor readings: never). A rule caps growth, it does not refund the past.",
                       r, est_month=None, tier="B",
                       do="Add an Expiration rule per prefix after checking who reads it (put-bucket-lifecycle-configuration REPLACES the whole config: get it first)",
                       undo="Remove the rule; expired objects do not come back")
        classes = r.get("classes", {})
        int_gb = sum(v for k, v in classes.items() if k.startswith("IntelligentTiering"))
        if int_gb > 1 and r.get("avg_object_kb") is not None and r["avg_object_kb"] < 128:
            mon = r["objects"] / 1000 * LIST_PRICE["s3_int_monitoring_per_1000_objects_month"]
            report.add("s3.int_on_small_objects", "s3", reg, name,
                       f"Intelligent-Tiering on objects averaging {r['avg_object_kb']} KB: below 128 KB objects are NEVER tiered down but the monitoring fee "
                       f"(~${mon:.0f}/month for {r['objects']:,} objects) is charged anyway. Move back to Standard.",
                       r, est_month=mon, basis="list", tier="A")
        if r["gb"] >= 100 and r.get("avg_object_kb") is not None and r["avg_object_kb"] >= 256 and classes.get("StandardStorage", 0) >= 100 and not int_gb:
            std = classes.get("StandardStorage", 0)
            report.add("s3.standard_large_objects", "s3", reg, name,
                       f"{std} GB in Standard with large objects (avg {r['avg_object_kb']} KB): Intelligent-Tiering or IA fits if access is rare; "
                       "check GetRequests/BytesDownloaded (request metrics) or server access logs before moving; retrieval is not free.",
                       r, est_month=std * (0.023 - 0.0125) * 0.5, basis="list", tier="B")
        if r.get("int_days0"):
            report.add("s3.int_days0_rule", "s3", reg, name,
                       "Lifecycle rule transitions to Intelligent-Tiering at Days:0: every NEW object pays a transition request forever. Set the storage class at upload time instead (PUT with StorageClass) and keep the rule only for the backlog.",
                       r, tier="B")
        if r.get("open_multipart_uploads"):
            report.add("s3.incomplete_multipart", "s3", reg, name,
                       f"{r['open_multipart_uploads']} incomplete multipart uploads (invisible in bucket size, billed anyway). Add AbortIncompleteMultipartUpload 7 days: zero risk, it never touches a completed object.",
                       r, tier="A")
        if r.get("versioning") == "Enabled" and r["gb"] >= 20 and not r.get("has_expiration"):
            report.add("s3.versioned_no_noncurrent_expiry", "s3", reg, name,
                       "Versioning enabled with no NoncurrentVersionExpiration: every overwrite keeps the old copy forever.", r, tier="B")
        if r.get("public_policy") and not r.get("block_public_all"):
            report.add("s3.public_bucket", "s3", reg, name,
                       "Bucket policy makes it public. Not a cost item (unless it is the egress source): a security one. Check ListBucket is not anonymous, and put CloudFront in front of the hot ones.",
                       {"gb": r["gb"]}, tier="info")
    no_abort = [r["bucket"] for r in rows if not r.get("has_multipart_abort")]
    if len(no_abort) > len(rows) * 0.5:
        report.add("s3.multipart_rule_missing_everywhere", "s3", "global", f"{len(no_abort)} of {len(rows)} buckets",
                   "Most buckets have no AbortIncompleteMultipartUpload rule. Add a 7-day rule to all of them (merge into the existing lifecycle config, never overwrite).",
                   {"sample": no_abort[:20]}, tier="A")


def check_global_edge(session, report: Report) -> None:
    # CloudWatch dashboards are account-wide (the same list from every region): count them once.
    try:
        cw = session.client("cloudwatch", region_name="us-east-1", config=BOTO_CFG)
        dash = list(_paginate(cw, "list_dashboards", "DashboardEntries"))
        report.inv("global", "cloudwatch_dashboards", [d["DashboardName"] for d in dash])
        if len(dash) > 3:
            report.add("cw.dashboards", "cloudwatch", "global", f"{len(dash)} dashboards",
                       f"First 3 dashboards are free, then $3/month each. Names: {', '.join(d['DashboardName'] for d in dash[:12])}. Save the body JSON before deleting.",
                       {"names": [d["DashboardName"] for d in dash]}, est_month=(len(dash) - 3) * LIST_PRICE["cw_dashboard_month"], basis="list", tier="A",
                       do="aws cloudwatch get-dashboard --dashboard-name <n> --query DashboardBody --output text > n.json ; aws cloudwatch delete-dashboards --dashboard-names <n>",
                       undo="put-dashboard --dashboard-body file://n.json")
    except (ClientError, BotoCoreError) as e:
        report.skip("cloudwatch.dashboards", "global", e)
    # CloudFront: disabled distributions that keep a WAF alive; WAF CLOUDFRONT scope.
    try:
        cf_ = session.client("cloudfront", region_name="us-east-1", config=BOTO_CFG)
        items = cf_.list_distributions().get("DistributionList", {}).get("Items", []) or []
        rows = [{"id": d["Id"], "enabled": d.get("Enabled"), "aliases": d.get("Aliases", {}).get("Items", []), "waf": bool(d.get("WebACLId"))} for d in items]
        report.inv("global", "cloudfront", rows)
        for d in rows:
            if not d["enabled"] and d["waf"]:
                report.add("cloudfront.disabled_with_waf", "network", "global", d["id"],
                           "Disabled distribution (free) still attached to a WAF web ACL (not free: ACL + rules monthly). Decide the distribution's fate first, then delete both.",
                           d, est_month=LIST_PRICE["waf_acl_month"] + 3 * LIST_PRICE["waf_rule_month"], basis="list", tier="B")
        waf = session.client("wafv2", region_name="us-east-1", config=BOTO_CFG)
        used = {d["id"] for d in rows if d["waf"] and d["enabled"]}
        for acl in waf.list_web_acls(Scope="CLOUDFRONT").get("WebACLs", []):
            attached = [d for d in items if d.get("WebACLId") and acl["ARN"] in d.get("WebACLId", "") and d.get("Enabled")]
            if not attached:
                report.add("waf.cloudfront_unattached", "network", "global", f"web ACL {acl['Name']}",
                           "CLOUDFRONT-scope web ACL not attached to any enabled distribution.", {"name": acl["Name"]},
                           est_month=LIST_PRICE["waf_acl_month"] + 3 * LIST_PRICE["waf_rule_month"], basis="list", tier="A")
    except (ClientError, BotoCoreError) as e:
        report.skip("cloudfront/waf", "global", e)
    # Route 53: empty zones and domains on auto-renew.
    try:
        r53 = session.client("route53", region_name="us-east-1", config=BOTO_CFG)
        zones = list(_paginate(r53, "list_hosted_zones", "HostedZones"))
        empty = [z for z in zones if z.get("ResourceRecordSetCount", 0) <= 2]
        report.inv("global", "route53", {"zones": len(zones), "empty_zones": len(empty), "est_usd_month": round(len(zones) * 0.5, 2)})
        if empty:
            report.add("route53.empty_zones", "misc", "global", f"{len(empty)} hosted zones with only NS+SOA",
                       "$0.50/month each and they answer NXDOMAIN to everything. If the registrar still delegates to one of them, deleting it takes the domain offline: check the delegation first.",
                       {"zones": [z["Name"] for z in empty[:30]]}, est_month=len(empty) * 0.5, basis="list", tier="B")
        dom = session.client("route53domains", region_name="us-east-1", config=BOTO_CFG)
        domains = dom.list_domains(MaxItems=100).get("Domains", [])
        report.inv("global", "route53_domains", [{"name": d.get("DomainName"), "auto_renew": d.get("AutoRenew"), "expiry": str(d.get("Expiry"))[:10]} for d in domains])
    except (ClientError, BotoCoreError) as e:
        report.skip("route53", "global", e)


# ---------------------------------------------------------------------------
# Orchestration and output
# ---------------------------------------------------------------------------
def scan_region(session, report: Report, region: str, days: int, deep: bool, workers: int) -> None:
    log(f"{region}: network")
    safe(report, "network", region, check_network, session, report, region, days)
    log(f"{region}: ec2/ebs")
    safe(report, "ec2_ebs", region, check_ec2_ebs, session, report, region, days)
    log(f"{region}: lambda")
    safe(report, "lambda", region, check_lambda, session, report, region, days, deep, workers)
    log(f"{region}: rds")
    safe(report, "rds", region, check_rds, session, report, region, days)
    log(f"{region}: containers")
    safe(report, "containers", region, check_containers, session, report, region, days)
    log(f"{region}: observability/misc")
    safe(report, "observability", region, check_observability_misc, session, report, region, days)


def render_markdown(report: Report, meta: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append(f"# aws-savings scan\n")
    L.append(f"Account `{meta['account']}` · regions: {', '.join(meta['regions'])} · window {meta['days']} days · "
             f"generated {NOW.strftime('%Y-%m-%d %H:%M UTC')} · Cost Explorer calls: {report.ce_calls} (~${report.ce_calls * 0.01:.2f})\n")
    L.append("> READ-ONLY inventory. Every row is a *candidate* with evidence and a rough estimate, not a decision. "
             "Estimates marked `bill` come from your own Cost Explorer unit prices; `list` are public list prices (us-east-1, ±10%).\n")

    ce = report.inventory.get("global", {}).get("cost_explorer", {})
    if ce:
        L.append("## Spend overview (RECORD_TYPE = Usage only: taxes and monthly RI fees excluded)\n")
        if ce.get("daily_median_usd") is not None:
            L.append(f"Median daily usage cost over 30 days: **${ce['daily_median_usd']:.2f}** (≈ ${ce['daily_median_usd'] * 30:,.0f}/month).\n")
        if ce.get("service_7d"):
            L.append("| Service | last 7d | prev 7d | Δ | note |\n|---|---:|---:|---:|---|")
            for r in ce["service_7d"][:25]:
                d = "monthly cadence" if r["monthly_cadence"] else (f"{r['delta_pct']:+.0f}%" if r["delta_pct"] is not None else "")
                L.append(f"| {r['service']} | {r['last7_usd']:.2f} | {r['prev7_usd']:.2f} | {d} | |")
            L.append("")
        if ce.get("region_7d"):
            L.append("Regions (last 7 days): " + ", ".join(f"{r['region']} ${r['usd']:.0f}" for r in ce["region_7d"][:12]) + "\n")
        if ce.get("usage_type_movers"):
            L.append("What moved, by usage type (last 7 days vs the 7 before; the line that explains a service's jump):\n")
            L.append("| usage type | last 7d | prev 7d | Δ $ |\n|---|---:|---:|---:|")
            for m in ce["usage_type_movers"][:15]:
                L.append(f"| {m['usage_type']} | {m['last7_usd']:.2f} | {m['prev7_usd']:.2f} | {m['delta_usd']:+.2f} |")
            L.append("")
        if ce.get("savings_plans_detail"):
            L.append("| Savings Plan | family | region | utilization | unused 14d | ends |\n|---|---|---|---:|---:|---|")
            for p in ce["savings_plans_detail"]:
                L.append(f"| {p['type']} …{p['arn_suffix']} | {p.get('family') or '-'} | {p.get('region') or '-'} | {p['utilization_pct']:.0f}% | ${p['unused_14d']:.2f} | {str(p.get('end'))[:10]} |")
            L.append("")
        cov = [c for c in ce.get("reservation_coverage_14d", []) if c["ondemand_hours"] > 0 or c["reserved_hours"] > 0]
        if cov:
            L.append("| Reservation coverage (14d) | type | covered | on-demand h | on-demand $ |\n|---|---|---:|---:|---:|")
            for c in cov[:20]:
                L.append(f"| {c['service'].split(' - ')[0]} | {c['instance_type']} | {c['coverage_pct']:.0f}% | {c['ondemand_hours']:.0f} | {c['ondemand_cost_14d']:.0f} |")
            L.append("")
    cal = report.inventory.get("global", {}).get("commitments_calendar", [])
    if cal:
        L.append("## Commitments calendar (the only moment an over-commitment becomes fixable)\n")
        L.append("| kind | type | region | payment | ends |\n|---|---|---|---|---|")
        for c in cal:
            L.append(f"| {c['kind']} | {c.get('type') or c.get('family') or '-'} ×{c.get('count', 1)} | {c.get('region') or '-'} | {c.get('payment') or '-'} | {str(c.get('end'))[:10]} |")
        L.append("")

    order = {"A": 0, "B": 1, "C": 2, "info": 3, "X": 4}
    tier_title = {"A": "Tier A · just do it (reversible, nothing lost, nobody notices)",
                  "B": "Tier B · needs an owner's confirmation (someone may still use it, or the data does not come back)",
                  "C": "Tier C · investigate first (a number is missing)",
                  "info": "Info · context, hygiene, opposite-direction findings",
                  "X": "Do NOT cut (looks like savings, is not)"}
    total_a = sum(f["est_usd_month"] or 0 for f in report.findings if f["tier_hint"] == "A")
    total_b = sum(f["est_usd_month"] or 0 for f in report.findings if f["tier_hint"] == "B")
    L.append(f"## Findings: {len(report.findings)} · rough estimate Tier A ≈ ${total_a:,.0f}/month · Tier B ≈ ${total_b:,.0f}/month\n")
    for tier in ("A", "B", "C", "info", "X"):
        fs = [f for f in report.findings if f["tier_hint"] == tier]
        if not fs:
            continue
        fs.sort(key=lambda f: -(f["est_usd_month"] or 0))
        L.append(f"### {tier_title[tier]}\n")
        L.append("| check | region | resource | est $/mo | basis | detail |\n|---|---|---|---:|---|---|")
        for f in fs:
            est = f"{f['est_usd_month']:.0f}" if f["est_usd_month"] is not None else "?"
            det = f["detail"].replace("|", "\\|").replace("\n", " ")
            L.append(f"| `{f['check']}` | {f['region']} | {f['resource'].replace('|', '/')} | {est} | {f['basis'] or ''} | {det} |")
        L.append("")

    L.append("## Inventory summary\n")
    for region, inv in sorted(report.inventory.items()):
        if region == "global":
            s3 = inv.get("s3")
            if s3:
                L.append(f"- **S3**: {s3['buckets']} buckets, {s3['total_gb']:,} GB (≈ ${s3['est_usd_month']:,.0f}/month), {s3['no_lifecycle']} without lifecycle, "
                         f"{s3['no_multipart_abort']} without multipart-abort rule, {s3['public_policy']} with a public policy")
                L.append("\n| bucket | region | GB | Δ GB/yr | objects | avg KB | classes | lifecycle | est $/mo |\n|---|---|---:|---:|---:|---:|---|---|---:|")
                for r in s3["rows"][:40]:
                    cls = ", ".join(f"{k.replace('Storage', '')}:{v}" for k, v in sorted(r["classes"].items(), key=lambda x: -x[1])[:3])
                    L.append(f"| {r['bucket']} | {r['region']} | {r['gb']:,} | {r['growth_gb_year']:+,.0f} | {r['objects']:,} | {r['avg_object_kb'] or '-'} | {cls} | "
                             f"{'exp' if r.get('has_expiration') else ('rules' if r.get('lifecycle_rules') else 'none')} | {r['est_usd_month']:.0f} |")
                L.append("")
            continue
        lam = inv.get("lambda", {})
        ec2 = inv.get("ec2", {})
        L.append(f"- **{region}**: Lambda {lam.get('functions', 0)} functions ({lam.get('zero_invocations', 0)} never invoked, {lam.get('deprecated_runtimes', 0)} deprecated runtimes"
                 + (f", SnapStart cache {inv['lambda_snapstart']['gb_cached']} GB ≈ ${inv['lambda_snapstart']['est_usd_month']:.0f}/mo" if inv.get("lambda_snapstart") else "")
                 + f"); EC2 {len(ec2.get('running', []))} running / {len(ec2.get('stopped', []))} stopped; NAT {len(inv.get('nat_gateways', []))}; "
                 f"EIP {inv.get('elastic_ips', {}).get('total', 0)} ({inv.get('elastic_ips', {}).get('idle', 0)} idle); public IPv4 {sum(inv.get('public_ipv4_by_attachment', {}).values())}; "
                 f"RDS {len(inv.get('rds_instances', []))}; ECR {inv.get('ecr', {}).get('repos', 0)} repos {inv.get('ecr', {}).get('gb_nominal', 0)} GB; "
                 f"log groups {inv.get('log_groups', {}).get('total', 0)} ({inv.get('log_groups', {}).get('stored_gb', 0)} GB); secrets {inv.get('secrets', {}).get('total', 0)}")
    L.append("")
    if report.skipped:
        L.append(f"## Skipped ({len(report.skipped)}): missing permission or unsupported in region\n")
        seen = set()
        for s in report.skipped:
            key = (s["check"], s["error"][:60])
            if key in seen:
                continue
            seen.add(key)
            L.append(f"- `{s['check']}` [{s['region']}]: {s['error']}")
        L.append("")
    L.append("---\n*Next step: hand `report.json` and this file to the aws-savings skill. It applies the traps (first-of-month, consolidation lag, "
             "memory=CPU, alias-preserved versions, 128 KB tiering floor…) and writes the tiered plan with do/undo commands. Nothing here has been changed.*\n")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only AWS cost-waste scanner (aws-savings).")
    ap.add_argument("--profile", help="AWS profile name (default: credential chain)")
    ap.add_argument("--regions", help="Comma-separated regions (default: all regions enabled for the account)")
    ap.add_argument("--days", type=int, default=14, help="Metric window in days (default 14)")
    ap.add_argument("--out-dir", default="./aws-savings-report", help="Output folder for report.json and report.md")
    ap.add_argument("--skip-ce", action="store_true", help="Make no Cost Explorer calls (they cost $0.01 each)")
    ap.add_argument("--skip-s3", action="store_true", help="Skip the S3 inventory")
    ap.add_argument("--skip-lambda-deep", action="store_true", help="Skip per-function provisioned-concurrency lookups")
    ap.add_argument("--skip-regions", action="store_true", help="Skip the per-region checks (run only Cost Explorer, commitments, edge and S3)")
    ap.add_argument("--workers", type=int, default=6, help="Parallelism for regions and per-resource calls")
    args = ap.parse_args()

    try:
        session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
        sts = session.client("sts", region_name="us-east-1", config=BOTO_CFG)
        ident = sts.get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        print(f"Cannot authenticate: {_short_err(e)}\nLog in first (aws sso login --profile <p>, or export credentials).", file=sys.stderr)
        return 2
    account = ident.get("Account", "?")
    log(f"authenticated as {ident.get('Arn', '?').split('/')[-1]} in account …{account[-4:]}")

    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        ec2 = session.client("ec2", region_name="us-east-1", config=BOTO_CFG)
        regions = sorted(r["RegionName"] for r in ec2.describe_regions(AllRegions=False).get("Regions", []))
    log(f"regions: {', '.join(regions)}")

    report = Report()
    t0 = time.time()
    if not args.skip_ce:
        log("cost explorer")
        safe(report, "cost_explorer", "global", check_cost_explorer, session, report, args.days)
    safe(report, "commitments", "global", check_commitments_calendar, session, report, regions)
    safe(report, "edge", "global", check_global_edge, session, report)
    if not args.skip_regions:
        with cf.ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(regions)))) as ex:
            list(ex.map(lambda r: scan_region(session, report, r, args.days, not args.skip_lambda_deep, args.workers), regions))
    if not args.skip_s3:
        safe(report, "s3", "global", check_s3, session, report, regions, args.workers)

    meta = {"account": f"…{account[-4:]}", "account_full": account, "regions": regions, "days": args.days,
            "generated": NOW.isoformat(), "elapsed_s": int(time.time() - t0), "ce_calls": report.ce_calls}
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "report.json"), "w") as fh:
        json.dump({"meta": meta, "findings": report.findings, "inventory": report.inventory, "skipped": report.skipped,
                   "unit_prices_from_bill": {k: round(v, 6) for k, v in report.unit_prices.items()}}, fh, indent=1, default=str)
    md = render_markdown(report, meta)
    with open(os.path.join(args.out_dir, "report.md"), "w") as fh:
        fh.write(md)
    log(f"done in {meta['elapsed_s']}s: {len(report.findings)} findings, {len(report.skipped)} skipped -> {args.out_dir}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
