# Beyond AWS

On the account this skill comes from, the spend outside AWS was as large as AWS itself, and
nobody was looking at it. When the user asks "what else", these are the places. Same method:
the bill, the resource, the proof it is unused.

## IoT connectivity (SIM carriers)

- **Find the per-SIM fee and the per-SIM traffic.** A SIM that has not transmitted for a year
  costs exactly the same as a live one. Cross the carrier's SIM list (ICCID/MSISDN, last
  session, lifetime bytes) with your device table: SIMs under contract in the field, SIMs in
  stock, SIMs in dead devices. Thresholds: no traffic for 180 days (medium risk), 365 days
  (low), "never born" (under 100 KB in its whole life, over 6 months old).
- **Ask the account manager whether a "suspended" state at reduced fee exists** before
  deactivating anything: it turns an irreversible cut into a reversible one.
- **Alarm on the prepaid balance.** Five weeks of autonomy and nobody watching is how thousands of
  devices go dark at once.
- **Per-MB rates differ by carrier and country.** Audio-heavy devices (1.5 GB/month) belong on
  the cheapest per-MB carrier; 60 MB/month sensors on the cheapest per-SIM fee.

## Frontend hosting (Vercel and similar)

- **Builds, not projects, are the cost.** A monorepo with eight apps rebuilds all eight on
  every commit unless an "ignored build step" (`turbo-ignore`) is set per project. Measured:
  17% of builds were needed. Try it on one project for a week first (`--fallback=HEAD^1`).
- **ISR writes and image transformations** have their own lines: a missing `sizes` attribute
  generates dozens of variants per image.
- **Seats**: one duplicate member and one invitation never accepted, billed for months.
- **Paid integrations** nobody inventoried (log drains, monitoring).
- A dead project costs nothing; delete it for hygiene, not savings.

## Serverless Postgres (Neon and similar)

- **There is no subscription; compute is the bill, and compute does not sleep if a cron pokes
  it.** A `*/5 * * * *` health check keeps a database at 1 CU 98% of the time. Measured: the
  most expensive database of the company served a joke site not deployed in 83 days.
- Fixed-CU endpoints vs autoscaling 0.25-1 CU.
- Egress: one GB a month leaving towards an unknown destination is worth a look.
- Five crons every minute in a product API keep the DB awake 99.5% of the time; that one is a
  product decision, not infrastructure.

## GPU clouds (RunPod and similar)

- **Stopped pods cost nothing; network volumes do.** Orphan volumes from finished training
  jobs, priced per GB-month.
- **But do not delete the dataset volume that exists to avoid re-downloading from S3.** One
  training session that re-fetched a terabyte-scale dataset from S3 cost 7× in egress what the GPU cost. Check
  the orchestration config actually mounts the volume (an empty `networkVolumeId` field re-
  downloads every time).
- Idle timeout per worker: 60 s vs 5 s is a real line on a busy endpoint.
- Cheapest fix for egress: keep the training dataset on an object store without egress fees
  (R2-style), next to the GPUs.

## Video / media platforms

- Quotas: at 100% of the minutes quota, uploads start failing. That is an outage, not a cost.
- Videos migrated in bulk and never played; stuck uploads in `inprogress`.

## CI (GitHub Actions and similar)

- Workflows that do not cancel the previous run when a new one starts (`concurrency` with
  `cancel-in-progress: true` on test/docs workflows, never on production deploys).
- macOS runners cost 10× Linux: build iOS on tags, not on every push.
- Robot accounts holding a paid seat; check deploy keys before removing them.
- Secret scanning / advanced security switched on the day of an incident and never budgeted.

## SaaS and subscriptions

- Domains on auto-renew that resolve nothing (brand defence vs. cost: a decision).
- Mailboxes forgotten since 2022 on a hosted mail service.
- Monitoring SaaS polling CloudWatch (that is the `GetMetricData` line on the AWS side).
- Serverless dashboards shipping a KB of telemetry per invocation to a third party.

## The one that dwarfs the others

If the company runs AI coding assistants or LLM APIs, the equivalent list price of a month of
usage may exceed the entire infrastructure bill. Find out whether it is metered or flat before
anything else; it is five minutes and it reframes the audit.
