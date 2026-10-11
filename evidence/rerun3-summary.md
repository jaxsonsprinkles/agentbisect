# Re-run 3 (calibrated baseline, budget medians, bad end 0ecffc0)

Target support-agent-demo, good `1b40b27`, bad `0ecffc0` (HEAD). DEMO_FLAKE=0, agent claude-haiku-5-5, judge claude-sonnet-5-5 via the llm-proxy
(concurrency 4, subscription backend). Baseline: one `agentbisect calibrate` at 1b40b27 (3 repeats per case, `evidence/baseline-1b40b27.json`),
never recalibrated. Investigation: smart strategy, `--all-signatures --all-categories`, 1 repeat for behavioural checks, 3 for budget checks,
seeded from rw1 trace e2dad535... with `--good 1b40b27 --bad 0ecffc0`. Compare: `agentbisect compare` per signature. State/logs in
`evidence/rerun3-smart/`, compare logs and plain/smart state in `evidence/rerun3-compare/`.

## Per-signature table (agentbisect summary)
| signature | outcome | revert test (before -> after) | smart vs plain (eval rounds) |
|---|---|---|---|
| call_args:issue_refund.amount | culprit b0be626692 (confidence medium) | PASSED: pass 0% -> 100% | 24 plain vs 18 smart |
| must_not_call | culprit 0ecffc0942 (confidence high) | PASSED: pass 0% -> 100% | 4 plain vs 3 smart |
| budget:tokens@refund_outside_policy | culprit 5cee99b624 (confidence medium) | inconclusive | 21 plain vs 21 smart |
| call_args:escalate_to_human.priority | known pre-existing failure — correctly refused | not run | n/a (no compare data) |
| budget:tokens@refund_within_policy | culprit 59c8b10ccb (confidence low) | FAILED: median tokens 39,306 -> 35,195, within budget 0/3 -> 0/3 | 54 plain vs 60 smart |
| budget:tokens@order_status | culprit 59c8b10ccb (confidence low) | FAILED: median tokens 20,405 -> 22,861, within budget 0/4 -> 0/4 | 84 plain vs 96 smart |

## Score against the private answer key (kept outside the repos)
| signature | found | verdict |
|---|---|---|
| call_args:issue_refund.amount | b0be626 | correct |
| must_not_call (cancel_order) | 0ecffc0 | correct |
| budget:tokens@refund_within_policy | 59c8b10 | correct (revert test failed: other token regression still inflates the category) |
| budget:tokens@order_status | 59c8b10 | INCORRECT (key: ec19917, the R1 token regression; revert test also failed) |
| budget:tokens@refund_outside_policy | 5cee99b | INCORRECT (not a regression commit; single case ro2 at 22,890 vs limit 22,702, borderline noise) |
| call_args:escalate_to_human.priority (es2) | none | correct (known pre-existing failure, refused) |

4 of 6 correct. No signature surfaced for R1's own commit.

## Notes
- Behavioural regressions (R3, R1b) are isolated cleanly with passing revert tests. Budget (token) signatures are the weak spot: R1 and R2 both inflate
  every category's tokens, so per-category medians still mix them. order_status is attributed to R2 rather than R1, and reverting 59c8b10 alone
  leaves it over budget (median 20,405 -> 22,861 vs good 8,866).
- refund_outside_policy (ro2) is a one-case signature right at the 1.5x limit; its culprit 5cee99b is a wrong, noise-driven answer and the revert test was inconclusive.
- No po/es/ca/ms budget signatures appeared this time (they did in run 2); cancel_order cases are absent at the good end so they have no baseline (excluded in calibration).
- es2 was excluded from the baseline (no passing run) and correctly refused as known pre-existing.
- smart vs plain rounds: smart won on the behavioural signatures (24 vs 18, 4 vs 3), tied on ro2 (21 vs 21) and LOST on both budget signatures
  (order_status 84 plain vs 96 smart; refund_within_policy 54 vs 60). Planner context 20-24k tokens vs 44-47k for the raw range. Same culprit both ways in all five.
- Throttling: 0 rate-limit events, 0 retries. Proxy errors: 1 (investigate), 2 invalid-output retries.
- Divergence unavailable on all runs (neatlogs TRACE_FINALIZATION_FAILED); the primary also logged an unconfirmed reply_contains signature (os3) that was dropped.
- Flakiness: confidence on budget signatures is low/medium; the primary R3 revert test still carries the "revert conflicts" flag.
- Cache: the proxy cache (`.agentbisect/llm_cache`) was shared with earlier runs, so some calls were replays. Calls this re-run:
  calibrate 136 (112 real CLI, 24 cached), investigate 659 (564 real, 96 cached), compare 1196 (94 real, 1102 cached): 1991 calls, 770 real.
  The compare round counts are therefore replayed, not freshly sampled, evaluations.
