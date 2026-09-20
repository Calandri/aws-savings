---
name: aws-savings
description: Audit an AWS account for money spent on nothing and produce a tiered, evidence-backed cut list (read-only; you never delete). Use when someone asks to cut AWS costs, review the AWS bill, explain a cost spike, find idle resources, check reserved instances / Savings Plans coverage, or clean up S3, Lambda SnapStart, NAT gateways, Elastic IPs, snapshots, ECR. Knows where to look, how to prove a resource is unused, and what looks like savings but is not.
argument-hint: "[--profile <aws profile>] [--regions a,b] [focus: lambda|s3|network|rds|reservations|all]"
---

# aws-savings

You are auditing an AWS account for waste. Your output is a **proposal list**, never an action:
every line has a price, the proof it is unused, a risk, the command to do it and the command
to undo it. **You do not delete, stop, modify or purchase anything.** The account owner decides.

This skill was distilled from a six-month cost-cutting run on a real production account
(about a thousand Lambda functions, hundreds of buckets, resources scattered across many regions). Roughly a third of
that bill was waste, and most of it was invisible from the console: resources that outlived what
they served, fees billed per version, tiering that costs more than the storage it tiers. The
method below is what found it; the traps are what would have found the wrong things.

## 0. Non-negotiables

1. **Read-only.** Only `describe-*`, `list-*`, `get-*`, `get-metric-*`, Cost Explorer reads.
   If the user wants something executed, hand them the command and let them run it.
2. **Three proofs before any candidate is listed as a cut:** what it costs (Cost Explorer),
   what it is (the resource API), proof it is unused (CloudWatch). Missing one → it goes to
   "investigate first", not to the cut list.
3. **The bill, not the price list.** Unit prices come from Cost Explorer grouped by
   `USAGE_TYPE` (cost ÷ quantity). List prices are a fallback and are labelled as such.
4. **Never propose the things in [references/do-not-cut.md](references/do-not-cut.md).**
   Read it before writing the report. It is as important as the cut list.
5. **No secrets or identifiers in what you produce for others.** Account ids, ARNs, IPs, bucket
   names are fine in the owner's private report; strip them from anything shared.

## 1. Run the scanner first

```bash
# read-only; ~15-30 Cost Explorer calls (~$0.30); a few minutes on a large account
python3 scripts/aws_savings_scan.py --profile <profile> [--regions eu-central-1,us-east-1] [--days 14]
# outputs: ./aws-savings-report/report.md (read this) and report.json (query this)
```

Flags: `--skip-ce` (zero Cost Explorer cost, no bill-derived prices), `--skip-s3`,
`--skip-lambda-deep` (skips per-function provisioned-concurrency lookups), `--workers N`.
If the credentials are SSO, `aws sso login --profile <p>` first. The scanner records every
permission error under "Skipped" instead of dying: read that section, it tells you what you
could not see.

The scanner gives you candidates with rough estimates and a `tier_hint`. It is a starting
point: **you** confirm each candidate with the checks in
[references/checks.md](references/checks.md), correct the tier, and fill the gaps the
scanner cannot (who owns it, what the product needs, what the bill says exactly).

## 2. Then reason, in this order

1. **Read the spend overview** (Usage-only, ending yesterday). Note the median day, the
   services that moved, the regions that should be empty and are not. Anything with a
   `monthly cadence` flag (Route 53, RI fees, Tax) is excluded from week-over-week deltas.
2. **Walk the findings by category** with the check catalog. For each candidate decide the
   tier (below), get the real price from `unit_prices_from_bill` in `report.json` when the
   usage type exists, and write do/undo commands with real ids.
3. **Look for what the scanner cannot see** (section 4): hairpin traffic, egress spikes,
   GetMetricData callers, over-memoried Lambdas, RI/SP mismatches, dead schedules.
4. **Write the "do not cut" section** for this account: every plausible-looking item you
   rejected and why. A reader will be tempted to cut everything; this section stops them.
5. **Write the "spend more to save" section**: functions timing out at low memory, an RI for
   an instance that is 100% on-demand, a dataset copy next to the GPU, a state-change alarm
   on a giant stopped instance.
6. **Order of execution**: Tier A first, then the questions (Tier B are emails, not commands),
   then the investigations. Put the commitment expiry dates on a calendar.

Report format: [references/report-template.md](references/report-template.md).

## 3. Tiers

| Tier | Meaning | Examples |
|---|---|---|
| **A · just do it** | reversible with one command, nothing lost, nobody notices | NAT with 0 bytes (keep the EIP), WAF attached to nothing, provisioned concurrency with 0 utilization, gp2→gp3, Container Insights on an empty cluster, SnapStart on a cron, old versions pruned |
| **B · needs an owner's confirmation** | saves money, but someone may still use it, or the data does not come back | old snapshots/AMIs, idle EIP (allowlists!), stopped instances, Transfer Family server, RI purchase, lifecycle expiration on raw data, secrets |
| **C · investigate first** | a number is missing: who calls it, what is inside, is it covered | high GetMetricData, unnamed EC2, detached volumes without tags, Aurora I/O-Optimized break-even, egress from a region with no compute |
| **X · do not cut** | looks like savings, is not | log retention, Intelligent-Tiering on small objects, GuardDuty, empty ECS clusters, All-Upfront RIs already paid, interface VPC endpoints "to save on NAT" |

Mark exactly one tier per item. If unsure between A and B, it is B.

## 4. Where the money hides (the short version; full catalog in references/checks.md)

**Lambda.** SnapStart cache is billed per *published version* × memory, 24/7, and deploy tools
keep old versions (prune plugins preserve alias-referenced versions *in addition* to the count
they keep). SnapStart on crons/workers buys nothing. Provisioned concurrency with zero
utilization for 90 days. Functions at 100% error rate pay to fail. Functions whose average
duration sits at the timeout deliver nothing: *more* memory often costs the same and works.
Memory right-sizing only on network-bound functions (memory = CPU: halving a CPU-bound function
doubles its duration and saves nothing). Staging schedules at production cadence.

**Reservations.** Utilization can be 100% while coverage is 0% on the one instance that costs
the most. Savings Plans cannot be cancelled after 7 days: their unused commitment is a fact,
and the only lever is the renewal date and not buying RIs on a family an SP already covers
(RIs apply *before* SPs, so the SP goes unused twice). Aurora RIs cover both Standard and
I/O-Optimized, but I/O-Optimized consumes 30% more normalized units. Never run the purchase
recommendation on a lookback window that includes things you just switched off.

**RDS/Aurora.** Manual snapshots of databases that no longer exist (`RDS:ChargedBackupUsage`
in an account with only Aurora clusters). I/O-Optimized wins only when I/O charges would be a
large share of the cluster bill: measure `VolumeReadIOPs + VolumeWriteIOPs` for 30 days,
switch once, wait 30 days. Serverless v2 that never sleeps because a pool holds a connection.
Database Insights "advanced" on a database nobody looks at. Public endpoints = billed IPv4 +
exposure. Backup retention of 1-3 days on production is the opposite finding: raise it.

**S3.** Never list big buckets: sizes and object counts come free from CloudWatch
(`BucketSizeBytes`, `NumberOfObjects`, per storage class, and 90 days ago for growth).
Average object size decides everything: under 128 KB, Intelligent-Tiering never tiers but
charges monitoring, and Glacier bills a 128 KB minimum plus 40 KB overhead (a bucket of
44 KB objects triples in size). Growth without an expiration rule is next year's bill. A
`Days: 0` transition rule taxes every new object forever. Incomplete multipart uploads are
invisible and billed. A lifecycle rule whose prefix no longer matches the keys has been
lying for years. Egress spikes are usually a training job re-downloading a dataset it already
has elsewhere, or a public bucket without a CDN.

**Network.** Resources outlive what they served: the ALB is deleted, the WAF stays (and its
Bot Control subscription); the Lambda died in 2020, its NAT gateway is still up; the instance
is gone, its Elastic IP is billed. Every public IPv4 costs the same idle or not: count them by
attachment (EC2, RDS public, ALB nodes, ECS tasks, NAT, endpoints). Gateway endpoints for S3
are free and often missing on the NAT's route table; interface endpoints cost ~$22/month per
AZ each, usually more than the NAT data they save. Hairpin traffic (a Lambda calling its own
public API Gateway through the NAT) is paid twice. Transfer Family is $0.30/h per protocol
whether or not bytes flow.

**Storage.** Detached volumes, gp2 that could be gp3, snapshots and AMIs of machines
terminated years ago (billed on unique blocks: `ebs list-snapshot-blocks` for the real size),
Lightsail snapshots with no instances, stopped instances that still pay disk and IP, a giant
stopped instance nobody may ever start (alarm it, do not just note it).

**Containers.** ECR without lifecycle policies or with "keep 20 untagged"; shared layers are
billed once (multiply nominal sums by ~0.7). Container Insights on empty clusters. App Runner
provisioned memory for 30 requests a day. A GPU instance on an All-Upfront RI: switching it
off refunds nothing, the decision is the renewal.

**Observability.** CloudWatch money is `GetMetricData` (a status page polling every 5 minutes,
a SaaS monitor), custom metrics, ingestion, Database/Container Insights. Log *storage* is
pennies: never propose retention "to save". Dashboards beyond 3. Alarms permanently in ALARM.
API Gateway execution logging at INFO with data trace on 60 stages (also a secrets leak).
Third-party serverless dashboards shipping a 1.6 KB payload per invocation.

**Regions.** Run the same checks in every region: the leftovers live where nobody looks
(an e-commerce stack from years ago in one region, two forgotten micro databases in another, dev container repos in a third). GuardDuty in
empty regions is the one thing that would notice someone else using them: keep it.

## 5. Measurement traps (full list in references/traps.md)

- **The 1st of the month is not an anomaly**: Tax + RI recurring fees land that day. Always
  filter `RECORD_TYPE = Usage`, and mark monthly-cadence services.
- **Cost Explorer lags ~15 hours** and the `Estimated` flag means "month not invoiced", not
  "day incomplete". Compare windows ending *yesterday*. To know how much of today is ingested,
  read a fixed-rate counter (NAT hours ÷ number of NATs = hours ingested).
- **Cost Explorer stops at the service.** To attribute a Lambda spike to a function: CloudWatch
  `Duration` × `MemorySize/1024` per function, rank by day-over-day delta.
- **Memory is CPU** on Lambda. **Prune keeps alias targets.** **Snapshots bill unique blocks.**
  **ECR bills shared layers once.** **RIs apply before Savings Plans.** **I/O-Optimized uses
  1.3 normalized units.** **`put-bucket-lifecycle-configuration` replaces the whole config.**
- **`LastAccessedDate` on secrets is best-effort**: a secret read from `.env` looks dead.
- **Old Elastic IPs may sit in a partner's allowlist**: delete the consumer first, release the
  address after a month of silence.
- **A cut half-done**: check the previous round's "done" items are actually done (a cron that
  was supposed to be slowed still runs every 5 minutes) before claiming the total.

## 6. Beyond AWS

Half the waste on a typical bill is not on AWS: IoT SIMs that stopped transmitting a year ago,
CI minutes rebuilding eight apps for a one-line change, a serverless Postgres kept awake by a
5-minute cron, GPU volumes orphaned by a training job, video quotas about to fail uploads.
When the user asks "what else", use [references/beyond-aws.md](references/beyond-aws.md).

## 7. Output checklist

- [ ] Every cut has: price, basis (bill/list), proof, risk, tier, do, undo.
- [ ] A "do not cut" section with the rejected look-alikes.
- [ ] A "spend more to save" section.
- [ ] The commitments calendar (RI and SP expiry dates).
- [ ] The permission gaps listed, with what they hide.
- [ ] Nothing was executed. Say so in the first paragraph.
