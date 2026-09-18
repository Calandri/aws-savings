#!/usr/bin/env python3
"""Offline smoke test: runs the scanner against a FAKE account.

No credentials, no network. Every AWS call is short-circuited with a `before-call` handler
that returns a canned response for the operation. botocore still validates the request
parameters against the real service model, so a wrong parameter name fails here, and the
canned resources are shaped to walk most branches of every check.

    python3 tests/smoke_fake_account.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.parse

import boto3
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import aws_savings_scan as scan  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)
OLD = NOW - dt.timedelta(days=900)
RECENT = NOW - dt.timedelta(days=3)
ACCOUNT = "123456789012"


def cw_datapoints(value: float):
    return {"Datapoints": [{"Timestamp": NOW, "Sum": value, "Average": value, "Maximum": value, "Minimum": value, "Unit": "Count"}], "Label": "x"}


def ce_rows(body: dict):
    days = [(NOW.date() - dt.timedelta(days=i)).isoformat() for i in range(14, 0, -1)]
    if body.get("GroupBy"):
        key = body["GroupBy"][0]["Key"]
        groups = {
            "SERVICE": ["AWS Lambda", "Amazon Relational Database Service", "Amazon Route 53", "EC2 - Other"],
            "REGION": ["eu-central-1", "us-east-1"],
            "USAGE_TYPE": ["EUC1-Lambda-SnapStart-Cached-GB-S", "EUC1-NatGateway-Hours", "EUC1-PublicIPv4:IdleAddress",
                           "RDS:ChargedBackupUsage", "EUC1-CW:GMD-Metrics", "EUC1-DataTransfer-Out-Bytes"],
        }.get(key, ["x"])
        out = []
        for i, d in enumerate(days):
            g = []
            for k in groups:
                cost = 20.0 if k != "Amazon Route 53" else (14.0 if i == 3 else 0.05)
                g.append({"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"},
                                                    "UsageQuantity": {"Amount": "48", "Unit": "N/A"}}})
            out.append({"TimePeriod": {"Start": d, "End": d}, "Groups": g, "Estimated": True})
        return {"ResultsByTime": out}
    return {"ResultsByTime": [{"TimePeriod": {"Start": d, "End": d}, "Total": {"UnblendedCost": {"Amount": "150.5", "Unit": "USD"},
                                                                              "UsageQuantity": {"Amount": "1", "Unit": "N/A"}}} for d in days]}


FAKES = {
    "GetCallerIdentity": {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/tester", "UserId": "AROA"},
    "DescribeRegions": {"Regions": [{"RegionName": "eu-central-1"}]},
    "GetReservationUtilization": {"UtilizationsByTime": [{"TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"},
                                                          "Total": {"UtilizationPercentage": "88.5", "UnusedHours": "120", "PurchasedHours": "3000", "NetRISavings": "200"}}]},
    "GetReservationCoverage": {"CoveragesByTime": [{"TimePeriod": {"Start": "x", "End": "y"}, "Groups": [
        {"Attributes": {"instanceType": "db.r7g.large"}, "Coverage": {"CoverageHours": {"OnDemandHours": "336", "ReservedHours": "0", "CoverageHoursPercentage": "0"},
                                                                     "CoverageCost": {"OnDemandCost": "145.49"}}}]}]},
    "GetSavingsPlansUtilization": {"SavingsPlansUtilizationsByTime": [{"TimePeriod": {"Start": "x", "End": "y"},
                                                                       "Utilization": {"TotalCommitment": "11.5", "UsedCommitment": "7.9", "UnusedCommitment": "3.6", "UtilizationPercentage": "68.8"}}]},
    "GetSavingsPlansUtilizationDetails": {"SavingsPlansUtilizationDetails": [{"SavingsPlanArn": "arn:aws:savingsplans::" + ACCOUNT + ":savingsplan/abcdef",
                                                                              "Attributes": {"SavingsPlansType": "EC2Instance", "InstanceFamily": "t2", "Region": "eu-central-1", "EndDateTime": "2027-06-04T00:00:00Z", "PaymentOption": "Partial Upfront"},
                                                                              "Utilization": {"UtilizationPercentage": "7.9", "UnusedCommitment": "19.7"}}]},
    "GetAnomalies": {"Anomalies": [{"AnomalyId": "a", "AnomalyStartDate": "2026-01-02", "AnomalyEndDate": "2026-01-02",
                                    "Impact": {"MaxImpact": 50.0, "TotalImpact": 52.0}, "RootCauses": [{"Service": "AWS Lambda", "Region": "eu-central-1", "UsageType": "EUC1-Lambda-GB-Second"}],
                                    "AnomalyScore": {"MaxScore": 1.0, "CurrentScore": 1.0}, "MonitorArn": "arn"}]},
    "DescribeSavingsPlans": {"savingsPlans": [{"savingsPlanType": "Compute", "commitment": "0.25", "paymentOption": "No Upfront", "end": "2028-04-10T00:00:00Z", "returnableUntil": "2025-04-18T00:00:00Z"}]},
    "DescribeReservedInstances": {"ReservedInstances": [{"InstanceType": "r7g.large", "InstanceCount": 1, "OfferingClass": "standard", "OfferingType": "All Upfront",
                                                         "Scope": "Availability Zone", "End": NOW + dt.timedelta(days=300), "State": "active"}]},
    "DescribeReservedDBInstances": {"ReservedDBInstances": [{"State": "active", "DBInstanceClass": "db.r6i.large", "DBInstanceCount": 1, "OfferingType": "Partial Upfront",
                                                             "MultiAZ": False, "StartTime": NOW - dt.timedelta(days=400), "Duration": 94608000}]},
    "DescribeNatGateways": {"NatGateways": [
        {"NatGatewayId": "nat-0dead", "VpcId": "vpc-1", "SubnetId": "subnet-1", "CreateTime": OLD, "Tags": [{"Key": "Name", "Value": "lambda-gw"}], "NatGatewayAddresses": [{"PublicIp": "203.0.113.10"}]},
        {"NatGatewayId": "nat-0live", "VpcId": "vpc-2", "SubnetId": "subnet-2", "CreateTime": OLD, "Tags": [], "NatGatewayAddresses": []}]},
    "DescribeAddresses": {"Addresses": [{"PublicIp": "203.0.113.11", "AllocationId": "eipalloc-1", "Tags": [{"Key": "Name", "Value": "old"}]},
                                        {"PublicIp": "203.0.113.12", "AllocationId": "eipalloc-2", "AssociationId": "eipassoc-1", "NetworkInterfaceId": "eni-1"}]},
    "DescribeNetworkInterfaces": {"NetworkInterfaces": [
        {"NetworkInterfaceId": "eni-1", "InterfaceType": "interface", "Description": "RDSNetworkInterface", "Association": {"PublicIp": "203.0.113.12"}},
        {"NetworkInterfaceId": "eni-2", "InterfaceType": "nat_gateway", "Description": "Interface for NAT Gateway nat-0dead", "Association": {"PublicIp": "203.0.113.10"}},
        {"NetworkInterfaceId": "eni-3", "InterfaceType": "interface", "Description": "ELB app/x/1", "Association": {"PublicIp": "203.0.113.13"}},
        {"NetworkInterfaceId": "eni-4", "InterfaceType": "interface", "Description": "", "Attachment": {"InstanceId": "i-run"}, "Association": {"PublicIp": "203.0.113.14"}}]},
    "DescribeVpcEndpoints": {"VpcEndpoints": [{"VpcEndpointId": "vpce-1", "VpcEndpointType": "Gateway", "ServiceName": "com.amazonaws.eu-central-1.s3", "VpcId": "vpc-2"},
                                              {"VpcEndpointId": "vpce-2", "VpcEndpointType": "Interface", "ServiceName": "com.amazonaws.eu-central-1.transfer.server", "VpcId": "vpc-2", "NetworkInterfaceIds": ["eni-9", "eni-10"]}]},
    "DescribeLoadBalancers": {"LoadBalancers": [{"LoadBalancerArn": f"arn:aws:elasticloadbalancing:eu-central-1:{ACCOUNT}:loadbalancer/app/idle-alb/abc", "LoadBalancerName": "idle-alb",
                                                 "Type": "application", "Scheme": "internet-facing", "CreatedTime": OLD, "AvailabilityZones": [{"ZoneName": "a"}, {"ZoneName": "b"}]}],
                              "LoadBalancerDescriptions": [{"LoadBalancerName": "classic-1", "CreatedTime": OLD, "Instances": []}]},
    "DescribeTargetGroups": {"TargetGroups": [{"TargetGroupName": "orphan-tg", "LoadBalancerArns": []}]},
    "ListWebACLs": {"WebACLs": [{"Name": "shop-waf", "Id": "1111", "ARN": f"arn:aws:wafv2:eu-central-1:{ACCOUNT}:regional/webacl/shop-waf/1111"}]},
    "ListResourcesForWebACL": {"ResourceArns": []},
    "GetWebACL": {"WebACL": {"Name": "shop-waf", "Id": "1111", "ARN": "arn", "DefaultAction": {"Allow": {}}, "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "m"},
                             "Rules": [{"Name": "bot", "Priority": 1, "Statement": {"ManagedRuleGroupStatement": {"VendorName": "AWS", "Name": "AWSManagedRulesBotControlRuleSet"}},
                                        "OverrideAction": {"None": {}}, "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "b"}}]}},
    "ListServers": {"Servers": [{"ServerId": "s-1234567890abcdef0", "State": "ONLINE"}]},
    "DescribeServer": {"Server": {"ServerId": "s-1234567890abcdef0", "State": "ONLINE", "Protocols": ["SFTP", "FTPS"], "EndpointType": "PUBLIC", "Arn": "arn"}},
    "DescribeInstances": {"Reservations": [{"Instances": [
        {"InstanceId": "i-run", "InstanceType": "t3.xlarge", "State": {"Name": "running"}, "LaunchTime": OLD, "Tags": [{"Key": "Name", "Value": "quiet-box"}],
         "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-att"}}], "PublicIpAddress": "203.0.113.14", "ImageId": "ami-used"},
        {"InstanceId": "i-stop", "InstanceType": "c5n.18xlarge", "State": {"Name": "stopped"}, "LaunchTime": OLD, "Tags": [{"Key": "Name", "Value": "Computing"}],
         "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-stop"}}], "PublicIpAddress": "203.0.113.15", "CpuOptions": {"CoreCount": 36, "ThreadsPerCore": 2},
         "StateTransitionReason": "User initiated (2019-08-01)"},
        {"InstanceId": "i-noname", "InstanceType": "t2.micro", "State": {"Name": "running"}, "LaunchTime": OLD, "Tags": [], "BlockDeviceMappings": []}]}]},
    "DescribeVolumes": {"Volumes": [{"VolumeId": "vol-att", "Size": 200, "VolumeType": "gp2", "State": "in-use", "CreateTime": OLD},
                                    {"VolumeId": "vol-stop", "Size": 50, "VolumeType": "gp2", "State": "in-use", "CreateTime": OLD},
                                    {"VolumeId": "vol-orphan", "Size": 20, "VolumeType": "gp3", "State": "available", "CreateTime": OLD, "Tags": []}]},
    "DescribeImages": {"Images": [{"ImageId": "ami-old", "Name": "backend-backup", "CreationDate": "2022-12-20T00:00:00.000Z",
                                   "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami", "VolumeSize": 100}}]}],
                       "imageDetails": [{"imageSizeInBytes": 12 * 1024 ** 3, "imageTags": ["v1"], "imagePushedAt": RECENT},
                                        {"imageSizeInBytes": 11 * 1024 ** 3, "imagePushedAt": OLD}] + [{"imageSizeInBytes": 10 ** 9, "imagePushedAt": OLD}] * 12},
    "DescribeSnapshots": {"Snapshots": [{"SnapshotId": "snap-ami", "VolumeSize": 100, "StartTime": OLD, "Description": "ami"},
                                        {"SnapshotId": "snap-manual", "VolumeSize": 320, "StartTime": OLD, "Description": "Disco di platino"}]},
    "GetLifecyclePolicies": {"Policies": []},
    "ListBackupPlans": {"BackupPlansList": []},
    "ListFunctions": {"Functions": [
        {"FunctionName": "api-prod-handler", "Runtime": "python3.13", "MemorySize": 2048, "Timeout": 30, "Architectures": ["x86_64"], "SnapStart": {"ApplyOn": "PublishedVersions"}},
        {"FunctionName": "cron-nightly-reconcile", "Runtime": "python3.13", "MemorySize": 2048, "Timeout": 900, "Architectures": ["x86_64"], "SnapStart": {"ApplyOn": "PublishedVersions"}},
        {"FunctionName": "broken-cron", "Runtime": "python3.8", "MemorySize": 512, "Timeout": 60, "Architectures": ["x86_64"]},
        {"FunctionName": "slow-comment", "Runtime": "python3.12", "MemorySize": 512, "Timeout": 900, "Architectures": ["x86_64"]},
        {"FunctionName": "never-called", "Runtime": "nodejs16.x", "MemorySize": 128, "Timeout": 3, "Architectures": ["x86_64"]}]},
    "ListVersionsByFunction": {"Versions": [{"Version": "$LATEST", "MemorySize": 2048}] + [{"Version": str(v), "MemorySize": 2048, "SnapStart": {"ApplyOn": "PublishedVersions"}} for v in range(1, 7)]},
    "ListAliases": {"Aliases": [{"Name": "snapstart", "FunctionVersion": "6"}]},
    "ListProvisionedConcurrencyConfigs": {"ProvisionedConcurrencyConfigs": [{"FunctionArn": f"arn:aws:lambda:eu-central-1:{ACCOUNT}:function:api-prod-handler:live",
                                                                             "RequestedProvisionedConcurrentExecutions": 1, "Status": "READY"}]},
    "DescribeLogGroups": {"logGroups": [{"logGroupName": "/aws/lambda/api-prod-handler", "storedBytes": 5 * 1024 ** 3},
                                        {"logGroupName": "/aws/lambda/ghost-function", "storedBytes": 1000, "retentionInDays": 7}]},
    "DescribeDBInstances": {"DBInstances": [
        {"DBInstanceIdentifier": "main-writer", "DBInstanceClass": "db.r6i.xlarge", "Engine": "aurora-mysql", "DBClusterIdentifier": "main", "PubliclyAccessible": True,
         "StorageType": "aurora-iopt1", "BackupRetentionPeriod": 3, "DatabaseInsightsMode": "standard"},
        {"DBInstanceIdentifier": "webgis-1", "DBInstanceClass": "db.t3.medium", "Engine": "aurora-postgresql", "DBClusterIdentifier": "webgis", "PubliclyAccessible": True,
         "StorageType": "aurora-iopt1", "BackupRetentionPeriod": 7, "DatabaseInsightsMode": "advanced", "PerformanceInsightsRetentionPeriod": 465}]},
    "DescribeDBClusters": {"DBClusters": [
        {"DBClusterIdentifier": "main", "Engine": "aurora-mysql", "StorageType": "aurora-iopt1", "BackupRetentionPeriod": 3},
        {"DBClusterIdentifier": "webgis", "Engine": "aurora-postgresql", "StorageType": "aurora-iopt1", "BackupRetentionPeriod": 7},
        {"DBClusterIdentifier": "sv2-awake", "Engine": "aurora-mysql", "StorageType": "aurora", "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.0, "MaxCapacity": 1.0}}]},
    "DescribeDBSnapshots": {"DBSnapshots": [{"DBSnapshotIdentifier": "analytics-before-deletion", "DBInstanceIdentifier": "analytics", "AllocatedStorage": 100, "SnapshotCreateTime": OLD}]},
    "DescribeDBClusterSnapshots": {"DBClusterSnapshots": [{"DBClusterSnapshotIdentifier": "incident-2025", "DBClusterIdentifier": "main", "AllocatedStorage": 456, "SnapshotCreateTime": NOW - dt.timedelta(days=300)}]},
    "DescribeRepositories": {"repositories": [{"repositoryName": "nopolicy-repo"}, {"repositoryName": "policy-repo"}]},
    "GetLifecyclePolicy": {"lifecyclePolicyText": "{}"},
    "ListClusters": {"clusterArns": [f"arn:aws:ecs:eu-central-1:{ACCOUNT}:cluster/empty"]},
    "DescribeClusters": {"clusters": [{"clusterName": "empty", "runningTasksCount": 0, "activeServicesCount": 0, "registeredContainerInstancesCount": 0,
                                       "settings": [{"name": "containerInsights", "value": "enabled"}]}]},
    "ListServices": {"ServiceSummaryList": [{"ServiceName": "tiny-dashboard", "ServiceId": "abc", "ServiceArn": "arn", "Status": "RUNNING"}]},
    "DescribeService": {"Service": {"ServiceName": "tiny-dashboard", "InstanceConfiguration": {"Cpu": "1024", "Memory": "2048"}}},
    "ListNotebookInstances": {"NotebookInstances": [{"NotebookInstanceName": "nb-open", "NotebookInstanceStatus": "InService", "InstanceType": "ml.t3.medium"}]},
    "ListEndpoints": {"Endpoints": []},
    "GetInstances": {"instances": []},
    "GetInstanceSnapshots": {"instanceSnapshots": [{"name": "site-2020", "sizeInGb": 320, "createdAt": OLD}]},
    "GetDiskSnapshots": {"diskSnapshots": []},
    "ListDashboards": {"DashboardEntries": [{"DashboardName": f"d{i}"} for i in range(5)]},
    "DescribeAlarms": {"MetricAlarms": [{"AlarmName": "rps-low", "StateValue": "ALARM", "StateUpdatedTimestamp": NOW - dt.timedelta(days=40)}]},
    "GetRestApis": {"items": [{"id": "abc123", "name": "prod-api"}]},
    "GetStages": {"item": [{"stageName": "production", "methodSettings": {"*/*": {"loggingLevel": "INFO", "dataTraceEnabled": True}}}]},
    "ListSecrets": {"SecretList": [{"Name": "dead/secret", "LastAccessedDate": OLD}, {"Name": "live/secret", "LastAccessedDate": RECENT}]},
    "ListKeys": {"Keys": [{"KeyId": "k1"}]},
    "DescribeKey": {"KeyMetadata": {"KeyId": "k1", "KeyManager": "CUSTOMER", "KeyState": "Disabled"}},
    "ListRules": {"Rules": [{"Name": "geo-staging-wildfire", "State": "ENABLED", "ScheduleExpression": "rate(5 minutes)"}]},
    "ListBuckets": {"Buckets": [{"Name": "raw-bursts"}, {"Name": "big-images"}, {"Name": "tiny-bucket"}]},
    "GetBucketLocation": {"LocationConstraint": "eu-central-1"},
    "GetBucketLifecycleConfiguration": {"Rules": [{"ID": "int", "Status": "Enabled", "Filter": {"Prefix": ""}, "Transitions": [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]}]},
    "GetBucketVersioning": {"Status": "Enabled"},
    "GetBucketPolicyStatus": {"PolicyStatus": {"IsPublic": True}},
    "GetPublicAccessBlock": {"PublicAccessBlockConfiguration": {"BlockPublicAcls": False, "IgnorePublicAcls": False, "BlockPublicPolicy": False, "RestrictPublicBuckets": False}},
    "ListMultipartUploads": {"Uploads": [{"Key": "a", "UploadId": "1"}] * 3},
    "ListDistributions": {"DistributionList": {"Items": [{"Id": "E1DISABLED", "Enabled": False, "WebACLId": f"arn:aws:wafv2:us-east-1:{ACCOUNT}:global/webacl/cf/2222", "Aliases": {"Items": []}}],
                                               "Marker": "", "MaxItems": 100, "IsTruncated": False, "Quantity": 1}},
    "ListHostedZones": {"HostedZones": [{"Id": "Z1", "Name": "empty.example.", "ResourceRecordSetCount": 2, "CallerReference": "x"}]},
    "ListDomains": {"Domains": [{"DomainName": "example.com", "AutoRenew": True, "Expiry": NOW + dt.timedelta(days=200)}]},
}

# S3 metric values: bucket -> (bytes, objects); raw-bursts has small objects in Intelligent-Tiering.
S3_SIZES = {"raw-bursts": (2500 * 1024 ** 3, 56_000_000), "big-images": (3000 * 1024 ** 3, 150_000), "tiny-bucket": (1024 ** 2, 10)}


def parse_body(body):
    """botocore has already serialized the body at before-call: JSON for json protocols, form-encoded for query ones."""
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        body = body.decode()
    if not body:
        return {}
    s = body.strip()
    if s.startswith("{"):
        return json.loads(s)
    return {k: v[0] for k, v in urllib.parse.parse_qs(s).items()}


def fake_handler(model, params, request_signer, context, **kwargs):
    op = model.name
    body = parse_body(params.get("body"))
    http = AWSResponse("https://fake.local", 200, {}, None)
    if op == "GetCostAndUsage":
        return http, ce_rows(body)
    if op == "GetMetricStatistics":
        b = body
        name = b.get("MetricName", "")
        dims = " ".join(str(d.get("Value", "")) for d in b.get("Dimensions", []) if isinstance(d, dict))
        if "nat-0dead" in dims or name in ("RequestCount", "ActiveFlowCount", "AllowedRequests", "BytesIn", "BytesOut", "Requests", "ProvisionedConcurrencyUtilization"):
            return http, cw_datapoints(0.0)
        if name == "CPUUtilization":
            return http, cw_datapoints(2.5)
        if name == "DatabaseConnections":
            return http, cw_datapoints(0.0 if "webgis" in dims else 40.0)
        if name in ("VolumeReadIOPs", "VolumeWriteIOPs"):
            return http, cw_datapoints(4e8)
        if name == "VolumeBytesUsed":
            return http, cw_datapoints(700 * 1024 ** 3)
        if name == "ServerlessDatabaseCapacity":
            return http, cw_datapoints(0.5)
        return http, cw_datapoints(1000.0)
    if op == "GetMetricData":
        results = []
        for mq in body.get("MetricDataQueries", []):
            qid = mq.get("Id", "")
            metric = (mq.get("MetricStat") or {}).get("Metric") or {}
            dims = [d.get("Value", "") for d in metric.get("Dimensions", [])]
            fn = dims[0] if dims else ""
            st = dims[1] if len(dims) > 1 else ""
            mname = metric.get("MetricName", "")
            vals = []
            if mname == "BucketSizeBytes":
                size, _ = S3_SIZES.get(fn, (0, 0))
                if size and ((fn == "raw-bursts" and st == "IntelligentTieringFAStorage") or (fn != "raw-bursts" and st == "StandardStorage")):
                    vals = [float(size), float(size) * 0.7]
            elif mname == "NumberOfObjects":
                _, n = S3_SIZES.get(fn, (0, 0))
                vals = [float(n), float(n) * 0.8] if n else []
            elif qid.startswith("inv_"):
                vals = [] if fn == "never-called" else [1000.0]
            elif qid.startswith("err_"):
                vals = [990.0] if fn == "broken-cron" else [1.0]
            elif qid.startswith("durmax"):
                vals = [900000.0] if fn == "slow-comment" else [2000.0]
            elif qid.startswith("dur_"):
                vals = [868000.0] if fn == "slow-comment" else [500.0]
            results.append({"Id": qid, "Label": qid, "StatusCode": "Complete", "Timestamps": [NOW] * len(vals), "Values": vals})
        return http, {"MetricDataResults": results, "Messages": []}
    if op == "GetLifecyclePolicy":
        if "nopolicy" in str(body):
            raise ClientError({"Error": {"Code": "LifecyclePolicyNotFoundException", "Message": "none"}}, op)
        return http, FAKES[op]
    if op == "GetPolicy":
        b = params.get("url_path", "")
        svc = "events.amazonaws.com" if "cron" in b else "apigateway.amazonaws.com"
        return http, {"Policy": json.dumps({"Statement": [{"Principal": {"Service": svc}}]})}
    if op == "GetBucketLifecycleConfiguration" and "tiny-bucket" in str(params.get("url_path", "")) + str(params.get("url", "")):
        raise ClientError({"Error": {"Code": "NoSuchLifecycleConfiguration", "Message": "none"}}, op)
    if op == "DescribeLoadBalancers":
        return http, {"LoadBalancers": FAKES[op]["LoadBalancers"]} if model.service_model.service_name == "elbv2" else {"LoadBalancerDescriptions": FAKES[op]["LoadBalancerDescriptions"]}
    if op == "DescribeImages":
        return http, {"Images": FAKES[op]["Images"]} if model.service_model.service_name == "ec2" else {"imageDetails": FAKES[op]["imageDetails"]}
    return http, FAKES.get(op, {})


def main() -> int:
    session = boto3.Session(aws_access_key_id="AKIAFAKEFAKEFAKEFAKE", aws_secret_access_key="fake", region_name="us-east-1")
    session._session.register("before-call.*.*", fake_handler)
    real_session_cls = boto3.Session
    boto3.Session = lambda *a, **k: session  # the scanner builds its own Session; hand it ours
    out = os.path.join(HERE, "..", ".smoke-out")
    sys.argv = ["scan", "--regions", "eu-central-1", "--out-dir", out, "--workers", "2"]
    try:
        rc = scan.main()
    finally:
        boto3.Session = real_session_cls
    assert rc == 0, f"scanner exited {rc}"
    data = json.load(open(os.path.join(out, "report.json")))
    checks = {f["check"] for f in data["findings"]}
    expected = {
        "nat.zero_traffic", "eip.idle", "ipv4.inventory", "vpce.missing_s3_gateway", "vpce.interface_cost", "lb.zero_requests", "lb.classic_idle",
        "waf.unattached", "transfer.idle_server", "ec2.underused", "ec2.unnamed", "ec2.stopped_with_ebs", "ec2.giant_stopped", "ebs.detached",
        "ebs.gp2_to_gp3", "ami.old_unused", "snapshot.old_manual", "backup.none", "lambda.always_failing", "lambda.timing_out",
        "lambda.unused_functions", "lambda.deprecated_runtimes", "lambda.snapstart_versions", "lambda.snapstart_on_cron", "lambda.provisioned_unused",
        "logs.storage_is_not_the_cost", "rds.public_endpoint", "rds.no_connections", "rds.insights_advanced", "rds.short_backup_retention",
        "aurora.io_optimized_check", "aurora.serverless_never_sleeps", "rds.old_manual_snapshot", "ecr.no_lifecycle", "ecs.insights_on_idle_cluster",
        "apprunner.low_traffic", "sagemaker.notebook_running", "lightsail.orphan_snapshots", "cw.dashboards", "cw.alarms_stuck", "apigw.verbose_logging",
        "secrets.stale", "kms.disabled_keys", "events.hot_nonprod_schedule", "s3.no_expiration_growing", "s3.int_on_small_objects",
        "s3.int_days0_rule", "s3.incomplete_multipart", "s3.versioned_no_noncurrent_expiry", "s3.public_bucket", "s3.multipart_rule_missing_everywhere",
        "cloudfront.disabled_with_waf", "route53.empty_zones", "ce.usage_type_signal", "ce.ri_coverage_gap", "ce.sp_unused", "ce.ri_utilization", "ri.zonal_scope",
    }
    missing = sorted(expected - checks)
    unexpected_skips = [s for s in data["skipped"] if "ParamValidation" in s["error"] or "TypeError" in s["error"] or "KeyError" in s["error"] or "AttributeError" in s["error"]]
    print(f"findings: {len(data['findings'])}  checks: {len(checks)}  skipped: {len(data['skipped'])}")
    for s in data["skipped"]:
        print("  skipped:", s["check"], s["region"], s["error"])
    if missing:
        print("MISSING checks:", ", ".join(missing))
    if unexpected_skips:
        print("BUGS (code errors recorded as skips):")
        for s in unexpected_skips:
            print("  ", s)
    md = open(os.path.join(out, "report.md")).read()
    assert "## Findings" in md and "Tier A" in md
    ok = not missing and not unexpected_skips
    print("SMOKE", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
