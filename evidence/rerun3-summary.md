# Re-run 3: calibrated baseline, median budgets, refusal, full range to 0ecffc0

Target support-agent-demo, good `1b40b27`, bad `0ecffc0`, started from the rw1 trace e2dad535 (`--good 1b40b27 --bad 0ecffc0`).
DEMO_FLAKE=0, agent claude-haiku-5-5, judge claude-sonnet-5-5, llm-proxy on the logged-in `claude` CLI (concurrency 4).
Steps: one `calibrate` at 1b40b27 (`baseline-1b40b27.json`, never recalibrated), `investigate --strategy smart --all-signatures
--all-categories` (budget 3 repeats, behavioural 1), then `compare` per signature. State and logs: `rerun3-smart/`, `rerun3-compare/`.

| signature | culprit found | revert test (before -> after) | plain vs smart rounds | vs private key |
|---|---|---|---|---|
| call_args:issue_refund.amount | b0be626 (medium) | PASSED, pass 0% -> 100% | 24 vs 18 | correct |
| must_not_call (proactive cancel_order) | 0ecffc0 (high) | PASSED, pass 0% -> 100% | 4 vs 3 | correct |
| budget:tokens@order_status | 59c8b10 (low) | FAILED, median tokens 20,405 -> 22,861 (good 8,866), within budget 0/4 -> 0/4 | 84 vs 96 | incorrect |
| budget:tokens@refund_within_policy | 59c8b10 (low) | FAILED, median tokens 39,306 -> 35,195, within budget 0/3 -> 0/3 | 54 vs 60 | correct (mixed signal) |
| budget:tokens@refund_outside_policy | 5cee99b (medium) | inconclusive | 21 vs 21 | incorrect |
| call_args:escalate_to_human.priority (es2) | none: known pre-existing failure, correctly refused (fails 3/3 at good end) | n/a | n/a | correct |

Score: 4 of 6 correct (key kept privately, not in either repo). Both single-regression behavioural signatures and the refusal were
right; the three token-budget signatures are the weak spot.

## Honest notes
- Budget signatures remain unreliable: every token regression (R1 tool schema, R2 search_help rule) inflates every category, so a
  per-category budget signature blames whichever commit crosses 1.5x last. order_status was assigned 59c8b10 although the
  order_status-specific regression is earlier; reverting 59c8b10 left tokens above the limit (revert test failed, flagged
  "multiple regressions likely"). refund_outside_policy landed on an unrelated later commit (5cee99b) with an inconclusive revert.
  refund_within_policy matches the key only because 59c8b10 is a contributor; its revert test also failed (0/3 -> 0/3).
- Smart was slower than plain on the two budget signatures that needed many rounds (+12, +6 rounds); it won on the two behavioural ones
  (-6, -1) and tied on one. Planner context 20-24k tokens with the graph vs 43-47k reading the raw range.
- A reply_contains signature (os3, 1 of 3 runs) was seen once and dropped as unreproduced (`signatures_unconfirmed`).
- ca/ms cases do not exist at the good end, so the baseline excludes them (and es2); no ca/ms/po/es budget signatures surfaced.
- Neatlogs span divergence still unavailable (TRACE_FINALIZATION_FAILED), so the planner ranked on graph impact and checkpoint intent.
- LLM proxy cache (`.agentbisect/llm_cache`, shared with earlier runs) replayed some identical prompts: calibrate 136 calls (112 real CLI,
  24 cache hits); investigate 659 calls (564 real, 96 hits, 1 error, 2 invalid-output retries); compare 5 signatures mostly cache
  replays (1079 calls with 88 real CLI by the third compare; final counter lost because the proxy stats file was written empty).
  Rate-limit events: 0, retries: 0 in calibrate and investigate; no 429 backoff was provoked.
- Not run: `compare` for es2 (refused, nothing to bisect).
