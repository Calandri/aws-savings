# Measurement traps

Every one of these produced a wrong number at least once during the real run this skill comes
from. Read them before trusting any figure, yours or the scanner's.

## Cost Explorer

**The 1st of the month is six times a normal day and nothing happened.** AWS books `Tax` and
the `Recurring` fees of reserved instances that day. A naive 7-day comparison that straddles
the 1st reports "-38%" one week and "+60%" the next. Always filter
`RECORD_TYPE = Usage`; show taxes and RI fees on a monthly line, never inside a weekly delta.

```bash
--filter '{"Dimensions":{"Key":"RECORD_TYPE","Values":["Usage"]}}'
```

**Some usage is monthly even with that filter.** Route 53 bills hosted zones on the 1st. Mark
as "monthly cadence" any service where one day is more than 60% of the week, and do not
compute its percentage change.

**The `Estimated` flag is useless.** It means "this month is not invoiced yet", so it stays
`true` on every day of the current month, including days that are two weeks old. Using it as
a reliability gate hides the whole current month.

**Cost Explorer lags about 15 hours, unevenly.** Compute counters trickle in during the day;
S3 storage counters land in one block at the end of it. The same day can be complete for one
service and empty for another. Compare windows that end *yesterday*. To know how much of
today has been ingested, read a fixed-rate counter: NAT gateway hours ÷ number of NATs = hours
ingested so far. If it reads 10 on a day with 2 NATs, only 5 hours of that day exist yet.

**Cost Explorer stops at the service.** It will not tell you which Lambda function or which
bucket. For Lambda: CloudWatch `Duration` (Sum, daily) × `MemorySize/1024` per function =
GB-seconds; rank by day-over-day delta. For S3 egress: server access logs or CloudFront logs;
request metrics per bucket cost money and are off by default.

**Each Cost Explorer request costs $0.01.** A full audit is around thirty cents. The Cost
Explorer *service* shows up in your own bill afterwards; that is you.

**Purchase recommendations look back 30-60 days.** Run them the day after you switched things
off and they will recommend commitments sized on what no longer exists. Wait 30 days after
the cuts settle. Prefer 1-year terms.

**A drop is a one-off as often as a spike is.** Lambda GB-seconds halving week over week looked
like a cut; it was the end of a bulk import that had inflated the previous week. Before crediting
a saving to a change, find the usage-type line that moved and check both weeks were normal.
The "what moved by usage type" table in the scanner report is the first thing to read.

**Cost Explorer rewrites closed days** when the month consolidates (credits, refunds,
reclassifications). If you store daily numbers, re-read the last 14 days each time.

## Lambda

**Memory is CPU.** A function gets ~1 vCPU at 1,769 MB. Halving the memory of a CPU-bound
function doubles its duration: same GB-seconds, zero savings, worse latency. Right-size only
functions that wait on the network (most of their time is I/O). Use `Max Memory Used` from
the REPORT lines (Logs Insights) and the average duration together.

**A function at its timeout is the most expensive thing you own.** It pays the full duration
and delivers nothing. Average duration close to the timeout with max exactly at it means the
fix is *more* memory (more CPU), which often ends up costing the same and working.

**SnapStart is billed per published version.** The cache fee is `versions × memory ×
$0.0000015/GB-s`, 24/7, and the restore fee is tiny by comparison. Deploy tools publish a
version on every deploy and never delete old ones. Old versions that nobody invokes for two
weeks drop out of the cache on their own; the recent ones all stay.

**The prune plugin keeps alias targets on top of the number you asked for.** With
`serverless-prune-plugin`, `number: 2` plus an alias means three versions survive. `number: 1`
gives you live + one rollback. `number: 0` is legal and leaves only the alias target.

**"Fast rollback" that updates `$LATEST` does nothing when traffic runs on an alias.** The API
invokes the alias, which points at an immutable version. Roll back with
`update-alias --function-version <previous>`, not with `update-function-code`.

**Zero-invocation functions cost zero.** Deleting 600 of them saves nothing today. The reason
to clean them up is hygiene: secrets, public URLs, deprecated runtimes, orphan log groups.

**Provisioned concurrency has its own usage type** (`Lambda-Provisioned-Concurrency`). It is
a flat daily line in the bill; if it never changes, nobody is scaling it, and
`ProvisionedConcurrencyUtilization` tells you whether anyone uses it at all.

## Reservations and Savings Plans

**Utilization and coverage are different questions.** Utilization 100% means every reserved
hour was consumed. Coverage 0% on one instance type means that instance is paid at full price
every hour. You can have both at once, and the second is where the money is.

**RIs apply before Savings Plans.** Buying RIs on an instance family that an EC2 Instance
Savings Plan already covers pushes the SP into unused commitment. The same three machines end
up paid twice.

**Savings Plans cannot be cancelled.** AWS refunds only within 7 days of purchase
(`returnableUntil`). After that, unused commitment is sunk until the end date. The only lever
is the calendar: do not renew at the same size.

**Standard RIs cannot be exchanged**, only resold on the RI Marketplace, which requires a US
bank account. Convertible RIs can be exchanged.

**All-Upfront RIs are already paid.** Switching off the instance saves nothing until the term
ends; it just loses the service. The decision is about the renewal.

**Aurora RIs cover both Standard and I/O-Optimized**, but I/O-Optimized consumes 30% more
normalized units (large 4→5.2, xlarge 8→10.4). Moving to Standard *increases* coverage. The
RI fees are billed on the 1st for the whole month, so in a mid-month view they look doubled.

**Zonal RIs only match instances in that AZ.** A node recreated in another AZ silently stops
being covered. `modify-reserved-instances` to `Scope=Region` is free.

## RDS / Aurora

**`RDS:ChargedBackupUsage` in an account with only Aurora clusters** means manual snapshots of
databases that no longer exist. Cost Explorer bills the used data, not the allocated size.

**I/O-Optimized wins only under heavy I/O.** It zeroes the I/O line in exchange for ~2.25×
storage and ~30% on instances. Measure `VolumeReadIOPs + VolumeWriteIOPs` for 30 days; the
switch is online, reversible, but allowed once every 30 days. Wrong guess = one month.

**Serverless v2 never scales to zero while a connection is open.** A pool, a monitor, a
5-minute cron: any of them pins the minimum. Look at `ServerlessDatabaseCapacity` Minimum over
two weeks; if it is never zero with `MinCapacity=0`, find the client.

**The backup window wakes a sleeping serverless DB every night.** Four non-zero ACU readings at
00:00 are not usage.

**A public endpoint is a billed IPv4 and an exposure.** Making it private breaks anyone who
connects from a laptop with an allowlisted IP. Decide with the users, not alone.

## S3

**Never `ls` a big bucket.** Sizes and object counts are free daily CloudWatch metrics
(`BucketSizeBytes` per storage class, `NumberOfObjects`). Listing 100 million objects costs
money and time and tells you less.

**Average object size decides the storage class.** Intelligent-Tiering never moves objects
below 128 KB to a cheaper tier, but charges monitoring per object regardless. Glacier and
Deep Archive bill a 128 KB minimum plus 40 KB of metadata per object: a bucket of 44 KB
objects is billed at about three times its size, plus the transition requests. A rule that was
right on a bucket of 250 KB photos is a loss on a bucket of 44 KB bursts.

**`Days: 0` transition rules tax every new object, forever.** They were meant for the backlog.
Set the storage class on upload instead.

**`put-bucket-lifecycle-configuration` replaces the whole configuration.** Read the existing
rules, merge, then put. Otherwise you delete rules you did not know about.

**A lifecycle rule can be dead for years.** A prefix filter that once matched the keys and no
longer does keeps the rule "Enabled" and does nothing. Compare the filter with actual key
names.

**Incomplete multipart uploads are invisible.** Not in the bucket size, not in listings, billed
anyway. `AbortIncompleteMultipartUpload` at 7 days has zero risk.

**Transition request spikes are one-offs.** The day you enable a lifecycle rule on a 5-million
object bucket, `Requests-Tier4` shows a big number. It is not an anomaly; the next day it is
back to cents. A *steady* Tier4 value means a `Days: 0` rule.

**Egress spikes have an author.** Two terabytes leaving S3 in two days is a training job that
re-downloaded a dataset it already had on another volume, or a cache that never hits. The
transport cost seven times the GPU that consumed it.

**Public buckets and egress are related but separate findings.** A public bucket without a CDN
pays internet egress at S3 prices; CloudFront in front of it costs less and can be cached.

**Storage counters for a bucket lag a day.** `BucketSizeBytes` is a daily metric; a bucket
emptied this morning still shows yesterday's size.

## EC2 / EBS

**Average CPU hides bursts.** Use a single CloudWatch period covering the whole window (a true
average, not an average of hourly averages) *and* the maximum. Average 3% with max 82% is a
bursty box that needs its headroom; average 0.5% with max 4% is a candidate.

**Stopped instances still pay disk and IP.** And a stopped 72-vCPU instance is a bill waiting
to happen: put an EventBridge rule on its state change, or terminate it after a snapshot.

**Snapshots bill unique blocks.** The nominal volume size is an upper bound; `ebs
list-snapshot-blocks` counts real blocks, but blocks shared along a snapshot chain are still
counted once each in the bill. The truth is between the two.

**Volumes without a Name tag are the leftovers of a cleanup**, not always the same one whose
snapshots you took. Check the ids before assuming they are covered.

**Detaching does not delete.** `describe-volumes --filters Name=status,Values=available` in
every region.

## Network

**Resources outlive what they served.** Deleting the load balancer leaves the WAF and its Bot
Control subscription. Deleting the Lambda leaves the NAT gateway created for its VPC. Deleting
the instance leaves the Elastic IP. None of them errors; all of them bill.

**Old Elastic IPs may be in someone's allowlist.** An address from 2019 can sit in a customer's
firewall. Delete the NAT/instance first, keep the address for a month, release it only after
nothing complained.

**Every public IPv4 is billed since February 2024**, attached or not. Count them by what they
belong to: EC2, RDS public endpoints, ALB nodes (one per AZ), ECS tasks with public IPs, NAT
gateways, VPC endpoints for Transfer Family.

**Interface VPC endpoints are not a savings tool.** ~$0.01/h per AZ each: on three AZs that is
~$22/month per service, usually more than the NAT data processing they remove. Gateway
endpoints (S3, DynamoDB) are free; check the route table they are attached to is the one the
private subnets actually use.

**Hairpin traffic is paid twice.** A Lambda in a private subnet calling its own public API
Gateway goes out through the NAT, through CloudFront, back into the same subnet. Invoke the
function directly, or pass the data in the payload.

**Transfer Family bills protocols, not bytes.** $0.30/h per enabled protocol per server,
around the clock. A server with SFTP and FTPS enabled for one client that uses SFTP pays
double.

## Containers and observability

**ECR bills shared layers once.** `describe-images` sums every image; the bill is ~0.7× that.
**Preview lifecycle policies** (`start-lifecycle-policy-preview`) before applying, and check
task definitions that pin images by digest.

**Empty ECS clusters are free.** Container Insights on them is not.

**CloudWatch money is not in log storage.** Storage is $0.03/GB-month; a 40 GB account pays
$16 a year for all its logs. Setting retention "to save" destroys the auth logs and the
upload logs you will need after the next incident, for the price of a lunch. The money is
`GetMetricData` (a status page polling 800 functions every 5 minutes), custom metrics,
ingestion, Database Insights advanced, Container Insights.

**An alarm permanently in ALARM is an alarm nobody sees.** Fix the threshold to the service's
real scale.

**`LastAccessedDate` on secrets is daily and best-effort.** A secret whose value was copied into
`.env` and a hosting platform's settings reads as "never accessed" and is very much alive.

## Process

**Half of what looks cuttable was cut last month.** Before proposing, verify with the
fixed-rate counters what already went to zero, or you double-count and re-propose. And verify
that the last round's "done" items are done: one of them was still running.

**Do not switch off what you have not understood.** An unnamed t2.micro from 2020 with SSH
open to the world is a security finding and an investigation, not a $69/year cut.

**Reversible first, questions second, irreversible last.** Snapshot before delete. Keep the IP.
Date the safety snapshots (90 days), then delete them.
