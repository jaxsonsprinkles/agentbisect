# Real re-run summary (signature bisect, weighted bisect, single repeat, throttling)

Target support-agent-demo, good `1b40b27` (verified: 18/19 executed cases pass at it; es2 fails there too, ca/ms skipped because
cancel_order does not exist yet), bad `f7d82b7` (HEAD before the tracing/flaky commits; all three regressions present). DEMO_FLAKE=0,
AGENT_MODEL=claude-haiku-5-5, JUDGE_MODEL=claude-sonnet-5-5 via the llm-proxy (concurrency 4, subscription backend), 1 repeat,
bad trace e2dad535... (rw1). Token baseline: merged calibration from 1b40b27 (3 repeats) plus ec19917 for the cancel_order cases
(`rerun2-baseline.json`). Without a baseline the harness skips token budgets, and R1/R2 are invisible (they only show as budget failures).

## Result per signature (run 2, after the fixes below)
| signature | culprit found | verdict | revert test |
|---|---|---|---|
| call_args:issue_refund.amount (R3, rw) | `b0be626` cents | CORRECT, confidence medium (revert conflicts flag) | passed 0% -> 100% |
| budget:tokens@refund_within_policy (rw) | `59c8b10` | plausible R2 (verify-with-search_help), confounded | failed 50% -> 50% |
| budget:tokens@order_status (os, R1 cases) | `59c8b10` | not a separate R1 culprit; same commit as above | failed 50% -> 0% |
| budget:tokens@policy_question (po, R2 cases) | `f7d82b7` | WRONG (borderline noise at the last commit) | failed 0% -> 50% |
| budget:tokens@escalation (es, R2 cases) | `ca150c7` | WRONG/unconfirmed | failed |
| budget:tokens@cancel_request (ca) | `96de44a` | unverified (revert inconclusive) | inconclusive |
| budget:tokens@multi_step (ms, R1 cases) | `22f7fbd` | unverified, 1 case, 1 sample | failed |
| call_args:escalate_to_human.priority (es2) | none | correctly refused: es2 fails at the good end too (not a regression) | n/a |

Success criteria: only R3 got its own culprit with a passing revert test. R1 and R2 were NOT separated: ground_truth.json only maps
cases to regressions (os/ms -> R1, po/es -> R2), not culprit commits, so the table scores case mapping and commit-message plausibility.
Every token regression inflates every category (the added tool schema and the search_help rule both lengthen every call), so
per-category budget signatures still mix R1 and R2 and the single-repeat token check is noisy near the 1.5x limit.

## Rounds
Primary (R3) investigation: 43 eval rounds including discovery. `compare` (separate empty caches): R3 plain 24 vs smart 21 rounds
(8 vs 7 steps), order_status budget plain 27 vs smart 24; same culprit both times. Smart wins by 3 rounds in both, but the
planner context was 21.7k tokens with the graph vs 43.3k reading the raw range (and 22.3k vs 39.4k). All 8 investigations: about 125
eval rounds plus compare reruns. Proxy totals for the whole session: 1417 calls (553 real CLI calls, 864 cache hits), 0 rate-limit
events, 0 backoff/recovered/gave_up events, 0 retries. No rate limiting was provoked at concurrency 4, so the live backoff path is
still only fake-tested.

## Bugs found and fixed (one commit each, 146 tests pass)
1. Budget regressions in different features collapsed into one `budget:tokens` signature; the suite only covered the bad trace's
   category (so R1/R2 cases never ran); one flaky rw3 run spawned three noise sibling investigations (call_args/must_call/call_counts).
   Fix: budget keys are `budget:tokens@<category>` (bare key still matches), `--all-categories`, signatures seen in a single run are
   re-run once and dropped if not reproduced (`signatures_unconfirmed`). Reviewer pass found three issues, all fixed.
2. A signature whose cases do not exist at the good end (cancel_order cases) made the good end "unusable". Now vacuously good.
   Resumed the two affected siblings; the other six ran on the code before this fix.
Run 1 (before fixes, `evidence/rerun1-prefix/`): R3 found at `b0be626`, budget at `59c8b10`, three noise siblings.

## Not verified / caveats
- Span divergence (neatlogs multi-span traces dead-letter, see neatlogs-dlq-report.md); intake picked good=d9b1680 from traces, overridden with --good.
- One sample per case per commit: budget verdicts near the limit flip, which is why po/es/ms culprits are unreliable. Raising `AGENTBISECT_REPEATS` was not tried.
- Haiku non-determinism (temperature cannot be set via the claude CLI); weighted-bisect optimality is not shown by 3-round differences.
- es2 and several cases are flaky/failing independent of the regressions.
