# Check catalog: where to look, what proves it, what it is worth

Conventions: `$A` is `aws --profile <profile>`; `<t>` today, `<t-14d>` fourteen days ago
(ISO 8601, UTC). Every command is read-only. Prices in the "worth" column are the public list
prices; take the real unit price from Cost Explorer (`cost ÷ quantity` of the usage type)
whenever the line exists in your bill. Region `us-east-1` for Cost Explorer, Savings Plans,
CloudFront and global WAF; everything else per region, **all regions**.

The scanner (`scripts/aws_savings_scan.py`) automates the inventory part of most checks below.
The "proof" and "trap" columns are what you still have to think about.

---

## A. The bill itself (Cost Explorer)

| Check | Command | What you learn |
|---|---|---|
| Daily usage-only spend, 30 days | `$A ce get-cost-and-usage --time-period Start=<t-30d>,End=<t> --granularity DAILY --metrics UnblendedCost --filter '{"Dimensions":{"Key":"RECORD_TYPE","Values":["Usage"]}}'` | the normal day; the 1st is Tax + RI fees and is excluded by the filter |
| Per service, 7d vs previous 7d | same, `--group-by Type=DIMENSION,Key=SERVICE`, windows ending yesterday | what moved; flag "monthly cadence" where one day > 60% of the week |
| Per region | `--group-by Type=DIMENSION,Key=REGION` | regions that should be empty and are not |
| Per usage type (the unit prices) | `--group-by Type=DIMENSION,Key=USAGE_TYPE --metrics UnblendedCost UsageQuantity` | cost ÷ quantity = the price you actually pay (NAT/h, EIP/h, GB-month, GB-s) |
| What moved, by usage type | same, 14 days DAILY, sum each week, sort by absolute delta | the one line that explains a service's jump (an egress burst, a tiering transition, a storage-type switch, a bulk job ending) |
| Anomalies | `$A ce get-anomalies --date-interval Start=<t-30d>,End=<t-1d>` (End must be yesterday) | root cause by service/region/usage type |
| Attribute a Lambda spike to a function | CloudWatch `Duration` Sum per function (period 86400) × `MemorySize/1024`, rank by day-over-day delta | Cost Explorer stops at the service |
| Forecast of the remaining month | `get-cost-forecast --granularity DAILY` and sum; `MONTHLY` on a partial period returns the whole month | |

Ingestion clock: `EUC1-NatGateway-Hours` (or any fixed-rate counter) quantity ÷ number of
resources = hours ingested for that day. Do not compare a day that is not complete.

## B. Reservations and Savings Plans

| Check | Command | Worth | Trap |
|---|---|---|---|
| RI utilization (unused hours) | `$A ce get-reservation-utilization --time-period Start=<1st of last month>,End=<1st of this month> --granularity MONTHLY` | unused hours × on-demand price | 100% utilization says nothing about coverage |
| RI coverage by instance type, per service | `$A ce get-reservation-coverage --time-period Start=<t-14d>,End=<t> --group-by Type=DIMENSION,Key=INSTANCE_TYPE --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Relational Database Service"]}}'` (also EC2, ElastiCache, OpenSearch) | `CoverageCost.OnDemandCost` × ~30% with a 1-year no-upfront RI | partial coverage on a writer after a move to Aurora I/O-Optimized: the RI covers fewer normalized units |
| Savings Plans utilization, daily | `$A ce get-savings-plans-utilization --time-period Start=<t-14d>,End=<t> --granularity DAILY` | unused commitment $/day × 365 is sunk, not recoverable | cannot be cancelled after 7 days |
| Per plan | `get-savings-plans-utilization-details` | which family is under-used and why (RIs bought later on the same family) | RIs apply before SPs |
| Inventory and expiry | `$A ec2 describe-reserved-instances --query 'ReservedInstances[?State==\`active\`].[InstanceType,OfferingClass,OfferingType,Scope,End]'`; `$A rds describe-reserved-db-instances`; `$A savingsplans describe-savings-plans` | the calendar | zonal RIs cover only one AZ |
| Purchase recommendation | `$A ce get-savings-plans-purchase-recommendation --savings-plans-type COMPUTE_SP --term-in-years ONE_YEAR --payment-option NO_UPFRONT --lookback-period-in-days THIRTY_DAYS` | | only 30 days AFTER the cuts settled; never 3 years |

## C. Lambda

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| SnapStart cache per version | `$A lambda list-functions --query 'Functions[?SnapStart.ApplyOn==\`PublishedVersions\`].FunctionName'`; per function `list-versions-by-function`, `list-aliases` | versions × MB/1024 × $0.0000015 × 86,400 × 30 per month | prune plugin keeps alias targets on top of `number`; traffic runs on the alias version, older ones serve nothing |
| SnapStart on non-API functions | trigger from `get-policy` (events/scheduler principal) or event source mappings; name hints (cron, worker) | the whole cache of that function | check the timeout margin before removing it |
| Provisioned concurrency unused | `list-provisioned-concurrency-configs --function-name <f>`; metric `ProvisionedConcurrencyUtilization` Maximum 90d | n × MB/1024 × $0.0000041667 × 86,400 × 30 | the function keeps working; it loses only the warm start |
| Always failing | `Errors` ≈ `Invocations` over 14d (get-metric-data, 500 queries per call) | small $, big reliability | a cron at 100% error has been broken for months |
| Timing out | `Duration` Average ≥ 70% of timeout and Maximum at the timeout | it pays and delivers nothing | fix = more memory (= more CPU), often same cost |
| Memory right-sizing | Logs Insights on the top-20 log groups, 3 days: `parse @message /Memory Size: (?<mem>\d+) MB\s+Max Memory Used: (?<used>\d+) MB/ \| filter ispresent(used) \| stats count(), max(used), avg(used), max(mem) by @log` | (allocated − used)/allocated × GB-s bill, ONLY on network-bound functions | memory is CPU |
| arm64 | `Architectures` in list-functions | ~20% of GB-s of the functions you move | binary deps (GDAL, numpy) must exist for arm64 |
| Never invoked | `Invocations` Sum 30-90d = 0 (or no datapoints) | $0 today; hygiene, secrets, runtimes | abandoned stacks, feature-branch deployments |
| Deprecated runtimes | `Runtime` vs the AWS deprecation schedule | risk, not cost | |
| Hot schedules in non-prod | `$A events list-rules` where `ScheduleExpression` is every 1-9 minutes and the name says staging/dev | invocations + logs + DB wake-ups | seasonality: a paused project is not a dead one |
| Third-party dashboards | `org:`/`app:` in `serverless.yml` shipping a ~1.6 KB payload per invocation | up to 86% of a function's log volume | it also leaves your account |

## D. EC2, EBS, snapshots

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| Real utilization | `$A cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUUtilization --dimensions Name=InstanceId,Value=<id> --start-time <t-14d> --end-time <t> --period 1209600 --statistics Average Maximum` | one size down ≈ 50% of the compute line | low average + high max = bursty, keep it; check RAM with the CW agent; check tags (`grant`, contracts) |
| Stopped instances | `describe-instances` state stopped: attached volumes, EIP | EBS + IP | a stopped 72-vCPU box needs an EventBridge alarm on `EC2 Instance State-change`, not a note |
| Detached volumes | `$A ec2 describe-volumes --filters Name=status,Values=available` in every region | GB × gp3 price | snapshot before delete; volumes without Name tag are not always the ones you think |
| gp2 → gp3 | `describe-volumes --filters Name=volume-type,Values=gp2` | 20% of the gp2 line, hot | > 1 TB volumes had more baseline IOPS on gp2 |
| Old snapshots and AMIs | `describe-snapshots --owner-ids self`; `describe-images --owners self`; real size `ebs list-snapshot-blocks --snapshot-id <s> --max-results 10000 --query 'length(Blocks)'` (×512 KiB) | $0.05/GB-month on unique blocks; archive tier $0.0125 (90-day minimum) | forensic/incident snapshots: ask; safety snapshots of a recent cleanup: date them |
| No backups at all | `$A dlm get-lifecycle-policies`; `$A backup list-backup-plans` | opposite finding | |
| Unknown machine (no Name, old) | snapshot the disk, read it from a temporary instance; SSM inventory; security groups | investigation | SSH open to the world on a 2019 system is the real finding |
| Lightsail | `get-instances` count vs `get-instance-snapshots`, `get-disk-snapshots` | $0.05/GB-month | keep the newest of a dead site |
| App Runner | `list-services`, `describe-service` memory; metric `Requests` 30d | provisioned GB × $0.007 × 730 | pause-service is reversible |
| SageMaker | `list-notebook-instances` InService; `list-endpoints` | per hour | stopped notebooks cost zero |

## E. RDS and Aurora

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| Manual snapshots of dead databases | `$A rds describe-db-snapshots --snapshot-type manual`; `describe-db-cluster-snapshots --snapshot-type manual`; bill line `RDS:ChargedBackupUsage` | $0.095/GB-month on used data | export to S3 Glacier IR (`start-export-task`) at ~20% instead of deleting |
| On-demand instance next to reserved ones | coverage by instance type (section B) | 30-40% of that instance | twins that both peak at 99%: keep both, reserve the on-demand one |
| I/O-Optimized break-even | `VolumeReadIOPs + VolumeWriteIOPs` (cluster dims) Sum 30d → I/O per month; Standard bills $0.20 per million; I/O-Optimized adds ~30% per instance and 2.25× storage | often $100-250/month on a mid-size cluster | `modify-db-cluster --storage-type aurora`, online, once per 30 days; RI coverage improves on Standard |
| Serverless v2 that never sleeps | `ServerlessDatabaseCapacity` Minimum 14d > 0 with MinCapacity 0; `DatabaseConnections` avg/max | min ACU × $0.12 × 730 | the client holding the connection; 15 s wake-up on first query |
| Zero-read instance | `DatabaseConnections` max 0, `ReadIOPS` 0, `SelectThroughput` 0 (PostgreSQL) | the instance | convert to Serverless v2 min 0 rather than delete: "not started yet" is not "dead" |
| Database Insights advanced | `DatabaseInsightsMode`, bill line `DatabaseInsights-vCPU-Hours` | vCPU × $0.0125 × 730 | history is lost on downgrade: export first |
| Public endpoints | `PubliclyAccessible` | one IPv4 each + exposure | breaks laptop access by allowlisted IP: decide with users |
| Backup retention | `BackupRetentionPeriod` < 7 on production | opposite finding | |
| Missing index / cache | 400 M reads vs 87 M writes in two weeks | moves the number more than any tariff | |

## F. S3

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| Sizes and object counts, free | CloudWatch `AWS/S3` `BucketSizeBytes` per `StorageType`, `NumberOfObjects` (`AllStorageTypes`), period 86400, in the bucket's region; 90 days ago for growth | | never `ls` a 100 M object bucket |
| Average object size | bytes ÷ objects | decides the storage class | < 128 KB: no Intelligent-Tiering, no Glacier |
| Lifecycle rules | `$A s3api get-bucket-lifecycle-configuration --bucket <b>` (NoSuchLifecycleConfiguration = none) | expiration caps growth; noncurrent-version expiry on versioned buckets | `put` REPLACES the whole config; a `Days:0` transition taxes every new object; a prefix filter can be dead |
| Growth | size now − size 90 days ago, × 4 | $/year of NEW spend | raw device data does not regenerate; derived tiles do |
| Intelligent-Tiering fee | bill line `Monitoring-Automation-INT`; $0.0025 per 1,000 objects/month | positive only when avg object > 128 KB and access is rare | monitoring fee vs storage saved |
| Incomplete multipart uploads | `$A s3api list-multipart-uploads --bucket <b>` | small $ but invisible and eternal | `AbortIncompleteMultipartUpload` 7 days on every bucket, zero risk |
| Egress | usage type group `S3: Data Transfer - Internet (Out)` over 12 months; per bucket only with request metrics or server access logs | $0.09/GB | a training job re-downloading a dataset; a public bucket without CDN; a client listing a bucket every 27 s |
| Public buckets | `get-bucket-policy-status`, `get-public-access-block`, anonymous `GET /?list-type=2` probe | security, and the egress source | CloudFront origins and website hosting before closing; code search is a weak proof |
| Stale buckets | serverless deployment buckets, `elasticbeanstalk-*`, `zappa-*`, `*-test`, empty buckets | hygiene | a bucket with zero bytes costs zero |
| Request storms | `AllRequests`/`ListRequests` request metrics (paid, per bucket) or server access logs for 24 h | small $, but a bug | |

## G. Network

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| NAT gateways | `describe-nat-gateways` in every region; `BytesOutToDestination`, `BytesInFromDestination`, `BytesOutToSource`, `ActiveConnectionCount` Sum 14d | $0.045/h + $0.045/GB | note subnet and routes first; KEEP the EIP; check which VPC and who lives in it (one dead Lambda?) |
| Elastic IPs idle | `describe-addresses` without `AssociationId`, every region | $3.65/month each | allowlists; release after a month of silence |
| All public IPv4 | `describe-network-interfaces --filters Name=association.public-ip,Values=*` grouped by description (ELB, RDS, NAT, ECS, endpoint) | $3.65/month each | RDS public → private is a product decision |
| S3 gateway endpoint | `describe-vpc-endpoints`: Gateway `.s3` on the route table the private subnets use | NAT data processing on S3 traffic | bucket policies restricted to the NAT IP break |
| Interface endpoints | count × ENIs × $0.01/h | usually more than the NAT data they save | keep for security, not for cost |
| Hairpin | flow logs for a week (`/vpc/flow-logs/...`), or Lambda logs calling your own public API domain | NAT bytes both ways | invoke directly or pass data in the payload |
| Load balancers | `elbv2 describe-load-balancers`; `RequestCount` (ALB) / `ActiveFlowCount` (NLB) 30d; classic `elb describe-load-balancers` | $16/month + one IPv4 per AZ | merging two live ALBs is a migration with downtime |
| Target groups without LB | `describe-target-groups` with empty `LoadBalancerArns` | $0 | tidiness only |
| WAF regional | `wafv2 list-web-acls --scope REGIONAL`; `list-resources-for-web-acl` (per resource type); `AllowedRequests` 14d; managed groups (Bot Control $10/month) | $5 + $1/rule + Bot Control | outlives the ALB |
| WAF CloudFront | `wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1`; `cloudfront list-distributions` `[Id,Enabled,WebACLId]` | same | a disabled distribution keeps the ACL alive |
| Transfer Family | `transfer list-servers`, `describe-server` Protocols; `BytesIn`/`BytesOut` 90d | $0.30/h per protocol | ingest path of field devices: replacing it is a project, not a cut |
| Regions | cost by REGION; `describe-*` in each: NAT, ALB, EIP, RDS, stopped EC2, ECR, secrets | leftovers of old projects | GuardDuty there is the one thing to keep |
| Route 53 | `list-hosted-zones` with `ResourceRecordSetCount` ≤ 2; `route53domains list-domains` AutoRenew | $0.50/zone | delegation and brand defence |

## H. Containers

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| ECR | `describe-repositories`; `get-lifecycle-policy` (LifecyclePolicyNotFoundException = none); `describe-images` sizes, untagged, pushedAt | $0.10/GB-month × ~0.7 (shared layers) | `start-lifecycle-policy-preview` first; task definitions pinning by digest; dead `dev` repos in other regions |
| ECS clusters | `describe-clusters --include SETTINGS STATISTICS`: `containerInsights` with 0 running tasks | custom metrics | empty clusters are free |
| Staging services in the prod cluster | services named staging with desired count 1 | one task 24/7 | same ALB, not isolated |
| GPU instance on RI | `describe-reserved-instances` g4dn/p3, `RequestCountPerTarget` | nothing until renewal | plan the migration for the renewal date |

## I. Observability and misc

| Check | Command / metric | Worth | Trap |
|---|---|---|---|
| Where CloudWatch money is | usage types `CW:GMD-Metrics`, `MetricMonitorUsage`, `DataProcessing-Bytes`, `VendedLog-Bytes`, `DatabaseInsights`, `TimedStorage-ByteHrs` | GetMetricData callers via CloudTrail data events / Athena | log storage is ~0.6% of CloudWatch |
| Log groups | `logs describe-log-groups`: stored bytes, retention, orphans of deleted functions | $0.03/GB-month | do NOT set retention to save money |
| Dashboards | `cloudwatch list-dashboards` | $3/month beyond 3 | `get-dashboard` body before deleting |
| Stuck alarms | `describe-alarms --state-value ALARM` with old `StateUpdatedTimestamp` | $0 | an always-red alarm hides the real one; fix the threshold |
| API Gateway logging | `get-stages` `methodSettings */*` loggingLevel INFO, dataTraceEnabled | ingestion $0.50/GB | bodies (and tokens) in logs; ERROR + dataTrace=false in prod |
| Secrets Manager | `list-secrets` LastAccessedDate > 180d | $0.40/month each | best-effort date; 30-day recovery window; a secret copied to `.env` looks dead |
| KMS | `list-keys` + `describe-key` KeyState Disabled, KeyManager CUSTOMER | $1/month each | data encrypted with it |
| Config, GuardDuty | `configservice describe-configuration-recorders`; `guardduty list-detectors` per region | GuardDuty: keep | |

## J. What the scanner does not do (do it by hand when it matters)

- Logs Insights memory query (costs money; run on the top-20 log groups only).
- `ebs list-snapshot-blocks` for real snapshot sizes (one call per snapshot).
- CloudTrail/Athena to find `GetMetricData` callers.
- S3 server access logs / request metrics to find who downloads or lists.
- Flow logs to find hairpin traffic and NAT destinations.
- Anything outside AWS: see `beyond-aws.md`.
