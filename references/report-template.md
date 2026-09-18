# Report template

Write the report for the person who pays the bill and will approve or reject each line, not
for the engineer who ran the scan. Numbers per year (or per month, but pick one), risk in
words, the command to do it and the command to undo it. Nothing in the report has been
executed; say so in the first lines.

```markdown
# AWS savings: <account alias> — <date>

**Perimeter:** <regions>, <window> of metrics, bill of <month>.
**Method:** read-only. Nothing was deleted, stopped, modified or purchased. Each line has the
command that proves it and the command that reverts it. Decisions are yours.
**Currency and prices:** USD from Cost Explorer (RECORD_TYPE = Usage); list prices where marked.

## 1. The total

| | $/year | Meaning |
|---|---:|---|
| Tier A · just do it | | reversible with one command, nothing lost |
| Tier B · needs a confirmation | | someone may still use it, or the data does not come back |
| Tier C · investigate first | | a number is missing |
| Already cut last round (do not re-propose) | | verified on fixed-rate counters |
| New spend avoided (growth capped) | | not a saving: an increase that will not come |

Two lines that matter: <the biggest reversible item> and <the biggest question>.

## 2. The list, by $/year

| # | What | $/year | Basis | Risk | Tier | Do | Undo |
|--:|---|---:|---|---|:-:|---|---|
| 1 | | | bill/list | | A | `aws ...` | `aws ...` |

## 3. Tier A: before executing
<the five things to know: note the subnet before deleting the NAT, keep the EIP, read the
lifecycle config before putting it, preview the ECR policy, note the Container Insights value>

## 4. Tier B: who has to answer, and the question
| # | Who | Question |
|---|---|---|

## 5. Tier C: what is missing and how to close it
| # | What is missing | How to close it, and what it costs |
|---|---|---|

## 6. Do not cut, even if it looks like savings
| What | Why not | The number |
|---|---|---|

## 7. Spend more to save
| What | Cost | Return | Why |
|---|---|---|---|
<function timing out at 512 MB, RI for the 100% on-demand database, dataset copy next to the
GPU, EventBridge alarm on the giant stopped instance, alarm on the prepaid balance of an IoT
carrier, expiration rules that cap growth>

## 8. Commitments calendar
| Kind | Type | Ends | Decision due |
|---|---|---|---|

## 9. What already went to zero (last round)
<fixed-rate counters before/after, so nobody re-proposes them>

## 10. In what order
| Order | What | $/year | Why first |
|---|---|---:|---|

## 11. What I could not see
<permission gaps, regions skipped, metrics without data points, and what they hide>
```

Conventions:

- **One number per item, with its proof next to it.** "NAT gateway: 0 bytes in 14 days on four
  metrics" beats "unused NAT gateway".
- **Mark bill-derived vs list prices.** Unit price = cost ÷ quantity from the usage type.
- **Split reversible from irreversible.** A snapshot before a delete moves an item from
  irreversible to reversible; say what it costs.
- **Do not count the same resource twice** across sections (a volume and the instance it was
  on; a WAF and the distribution it protects; Database Insights and the instance conversion
  that makes it moot).
- **State the exchange rate** if the reader thinks in another currency; it is an assumption.
- **Name the people** who must answer the Tier B questions. Those are emails, not commands.
- **Close with "what I did not measure and why."** A limit stated is worth more than a number
  guessed.
