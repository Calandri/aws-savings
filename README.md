# aws-savings

**A Claude Code skill that audits your AWS account for money spent on nothing, and tells you
exactly where to look, how to prove it, and what looks like savings but is not.**

It is read-only. It never deletes, stops, modifies or buys anything. The output is a tiered
proposal list: every line with a price, the proof the resource is unused, the risk, the
command to do it and the command to undo it. You decide.

It was distilled from a six-month cost-cutting run on a real production account (about a
thousand Lambda functions, hundreds of buckets, resources scattered across many regions). Roughly a third
of that bill was waste, and most of it was invisible from the console:

- a NAT gateway up since 2019 for a Lambda that last ran in 2020
- a WAF (with its Bot Control subscription) protecting a load balancer deleted months earlier
- Lambda SnapStart cache billed on six published versions per function, of which one served traffic
- provisioned concurrency kept warm 24/7 on functions with zero invocations in 90 days
- 1.7 TB of manual RDS snapshots of databases that no longer existed
- a read replica at 100% on-demand next to two reserved ones
- a Savings Plan at 8% utilization because reserved instances had been bought on the same family
- Intelligent-Tiering about to be "extended" to buckets of 44 KB objects, where it would have cost 2× the storage
- a status page calling `GetMetricData` 300 times an hour

## What is in the box

```
SKILL.md                      the skill: method, tiers, where the money hides, traps (short form)
scripts/aws_savings_scan.py   read-only boto3 scanner -> report.md + report.json
references/checks.md          the full catalog: check, command, worth, trap (Cost Explorer, RI/SP,
                              Lambda, EC2/EBS, RDS/Aurora, S3, network, containers, observability)
references/traps.md           measurement traps that produced wrong numbers at least once
references/do-not-cut.md      the look-alikes: things that seem like savings and are not
references/report-template.md the report the account owner will read
references/beyond-aws.md      IoT SIMs, CI minutes, serverless Postgres, GPU volumes, SaaS
tests/smoke_fake_account.py   offline test against a fake account (no credentials, no network)
```

## Install

As a personal skill for Claude Code (all projects):

```bash
git clone https://github.com/Calandri/aws-savings ~/.claude/skills/aws-savings
```

Or per project: clone into `<project>/.claude/skills/aws-savings`. The folder name is the skill
name; Claude Code picks it up at the next session. You can also zip the folder and upload it
as a skill on claude.ai.

Requirements for the scanner: Python 3.9+, `boto3` (`pip install boto3`), AWS credentials with
read-only permissions (`ReadOnlyAccess` plus `ce:Get*` for Cost Explorer is enough).

## Use

In Claude Code, ask in plain words:

> Audit this AWS account for waste, profile `prod`, all regions.

> Why did the Lambda bill jump on Tuesday?

> Are our reserved instances and Savings Plans actually used?

> Which S3 buckets are growing without a lifecycle rule?

The skill runs the scanner first, then reasons over the findings with the check catalog and
the traps, and writes the tiered report. Or run the scanner yourself:

```bash
python3 scripts/aws_savings_scan.py --profile prod                     # all enabled regions
python3 scripts/aws_savings_scan.py --profile prod --regions eu-central-1,us-east-1 --days 14
python3 scripts/aws_savings_scan.py --profile prod --skip-ce            # zero Cost Explorer calls
# -> ./aws-savings-report/report.md and report.json
```

Cost of a run: Cost Explorer charges $0.01 per request; a full run makes 15-30 of them.
CloudWatch `GetMetricData` is $0.01 per 1,000 metrics. A large account costs well under a dollar.

## What it checks

| Area | Examples |
|---|---|
| **The bill** | usage-only daily spend (the 1st of the month is Tax + RI fees, not an anomaly), per service with monthly-cadence flags, per region, unit prices derived from your own usage types |
| **Reservations** | RI utilization vs coverage per instance type, Savings Plans unused commitment, zonal RIs, the expiry calendar |
| **Lambda** | SnapStart cache per published version, SnapStart on crons, unused provisioned concurrency, functions at 100% errors, functions at their timeout, memory (with the memory-is-CPU trap), arm64, deprecated runtimes, hot non-prod schedules |
| **EC2 / EBS** | CPU average AND max over a single window, stopped instances still paying disk and IP, giant stopped instances, detached volumes, gp2→gp3, old snapshots and AMIs, no backup automation |
| **RDS / Aurora** | manual snapshots of deleted databases, on-demand instances next to reserved ones, I/O-Optimized break-even, Serverless v2 that never sleeps, Database Insights advanced, public endpoints, short backup retention |
| **S3** | sizes and counts from free CloudWatch metrics (never `ls`), average object size, lifecycle and expiration, 90-day growth, Intelligent-Tiering on small objects, `Days:0` rules, incomplete multipart uploads, versioning without noncurrent expiry, public policies |
| **Network** | NAT gateways with zero traffic (four metrics), idle Elastic IPs, every public IPv4 by attachment, missing S3 gateway endpoints, interface endpoint cost, idle load balancers, WAF attached to nothing, disabled CloudFront distributions with a WAF, Transfer Family protocol fees |
| **Containers** | ECR without lifecycle or with loose ones (shared layers billed once), Container Insights on empty clusters, App Runner for a handful of requests, SageMaker notebooks left running, Lightsail snapshots without instances |
| **Observability** | where CloudWatch money really is (GetMetricData, ingestion, Insights; not log storage), dashboards beyond 3, alarms stuck in ALARM, API Gateway INFO logging with data trace, stale secrets, disabled KMS keys |

And, as important, a list of **what not to cut**: log retention, Intelligent-Tiering on small
objects, Glacier on small objects, GuardDuty in empty regions, empty ECS clusters, Savings
Plans (cannot be cancelled), All-Upfront reservations, interface endpoints "to save on NAT",
the Elastic IP of a resource you deleted today, the final snapshot of a decommissioned
database.

## Tiers

| Tier | Meaning |
|---|---|
| **A** | just do it: reversible with one command, nothing lost, nobody notices |
| **B** | needs an owner's confirmation: someone may still use it, or the data does not come back |
| **C** | investigate first: a number is missing (who calls it, what is inside, is it covered) |
| **X** | do not cut: looks like savings, is not |

## Safety

- The scanner only calls `describe-*`, `list-*`, `get-*` and CloudWatch/Cost Explorer reads.
  Grep it: there is no `delete`, `put`, `modify`, `stop`, `terminate`, `purchase` call.
- Every permission error is recorded under "Skipped" in the report instead of aborting.
- The report is for you: it contains your resource ids. Strip them before sharing.
- The skill instructs the model to hand you commands, never to run them.

## Test

```bash
python3 tests/smoke_fake_account.py
```

Runs the whole scanner against a canned fake account with no credentials and no network.
botocore still validates every request against the real service models, so a wrong parameter
name fails here.

## Contributing

A new check needs three things: the command that finds the resource, the metric that proves it
is unused, and the trap that would make the number wrong. Add it to `references/checks.md`,
and to the scanner if it can be automated. Anything that turned out *not* to be a saving goes
to `references/do-not-cut.md`: that file is what makes the audit trustworthy.

## License

MIT.
