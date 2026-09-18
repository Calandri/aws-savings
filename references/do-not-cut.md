# Do not cut (it looks like savings, it is not)

Every audit produces a list of tempting items that would cost more than they save, destroy
evidence, or refund nothing. Reject them explicitly in the report, with the number, so the
reader is not tempted later. These are the recurring ones.

| Looks like | Why not | The number |
|---|---|---|
| **Set retention on the log groups that have none** | Log storage is $0.03/GB-month. You delete forever the authentication logs, the upload logs, the replication logs you need after the next incident | An account with 40 GB of logs pays ~$16/year for all of them |
| **Delete the log groups of functions that no longer exist** | They hold kilobytes | Worth $0. It is tidiness, not savings |
| **Extend Intelligent-Tiering to every bucket** | Objects under 128 KB never tier down; the per-object monitoring fee is charged anyway | A bucket of 80 M objects of 50 KB: $100/month of storage, $200/month of monitoring |
| **Glacier / Deep Archive on small-object buckets** | 128 KB minimum billable size + 40 KB metadata per object, plus transition requests | 56 M objects of 44 KB are billed as 6.8 TB instead of 2.3 TB, plus $560 of transitions |
| **Stopped SageMaker notebooks with big volumes** | They cost nothing while stopped | $0. Look at them for a different reason: one may be a `ml.g5.48xlarge` someone could start |
| **Empty ECS clusters, orphan target groups, spare ENIs** | Free | $0. Counting them inflates the total |
| **GuardDuty in regions with nothing in them** | Those are exactly the regions where an intruder would start machines unnoticed; and buckets do live there | A few dollars a month per empty region |
| **Cancel a Savings Plan** | Cannot be done after 7 days from purchase | The unused commitment is sunk until the end date; the lever is the renewal |
| **Switch off an instance on an All-Upfront RI** | Already paid; switching off refunds nothing and loses the service | Decide at renewal, use the remaining months to migrate |
| **Delete one of two read replicas that look half-idle** | Both peak at 99% at the same hours; with one, every peak is saturation | Buy the RI for the on-demand one instead |
| **Add interface VPC endpoints "to save on NAT"** | ~$0.01/h per AZ per service | ~$22/month per service on 3 AZs vs. a NAT data bill often smaller than that |
| **Release the Elastic IP of a deleted NAT/instance the same day** | The address may sit in a customer's or partner's allowlist; releasing is irreversible | $3.65/month to keep it for a month of silence |
| **Delete the final snapshot of a decommissioned database** | It is the parachute; nothing else holds that data | A 245 GB Aurora snapshot is $5/month; revisit when it turns three |
| **Delete the safety snapshots of last month's cleanup** | They are the rollback of a cleanup done yesterday | Date them (90 days), then delete |
| **Delete a snapshot named after an incident or a migration** (`post-hacking`, `before-migrating`) | Possible forensic or legal value; ask whoever handled it | Tens of dollars a year each |
| **Replace a managed SFTP endpoint with a self-hosted one because the fee is high** | It is the ingest path of field devices; a day lost is data lost; patches, certificates and HA become your job | Compare the yearly fee with two days of an engineer, every year |
| **Reduce memory on the function that uses 85% of it** | It is already at the limit; you would make it slower or OOM | Zero |
| **Reduce memory on a CPU-bound function** | Memory is CPU; duration doubles, GB-seconds unchanged | Zero, plus a slower product |
| **Delete stopped GPU pods / dead PaaS projects "to save"** | They do not bill while stopped | $0. Do it for hygiene: they contain cleartext keys |
| **Shrink a raw-data bucket with an expiration rule without asking** | Raw sensor readings and field photos do not regenerate; derived tiles do | The decision is per bucket, not global |
| **Delete a public bucket that "has zero references in the code"** | It may be the origin of a CDN distribution; code search is a weak proof | Check CloudFront origins and website hosting first |
| **Delete hosted zones that answer NXDOMAIN** | If the registrar still delegates to one, the domain goes dark; recreating changes the name servers | $0.50/month each |
| **Let unused domains expire** | The name is registered by someone else the next day | Brand defence, not infrastructure |
| **Delete the one bucket metrics configuration** | It is the only instrument measuring the request storm you are investigating | Remove it after the investigation |
| **Disable the alarms on rotated DB credentials** | They are the only way to notice a rotation broke something | Keep them; disable the staging ones |

Also list, in the same section, the items you *considered* and rejected for this account
specifically, with the number that killed them. The section is as valuable as the cut list:
a reader with a list of cuts is tempted to do them all.
