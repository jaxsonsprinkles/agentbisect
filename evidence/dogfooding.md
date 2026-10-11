# Dogfooding: AgentBisect run on its own demo, before and after

Same target (support-agent-demo), same tool, two runs of the same investigation. Nothing here is new work; it packages existing traces and reports.

## Run 1 (first end-to-end, bad trace rw1)
- Report: `report-6857f0b738.md` (BUILD_LOG "First real end-to-end run"). Range good `2f49adb` to bad `f7d82b7`.
- Named **59c8b10 (R2)**, confidence low, "multiple regressions likely", revert test failed. The actual cause of the failing check (refund amount 6400 vs 64.0) is R3 `b0be626`, so this culprit was wrong: overlapping regressions were mixed.
- Rounds: plain 36 vs smart 84. Smart was worse.

## Fix made because of it: bisect on the failure signature
The predicate became "does THIS check (`call_args:issue_refund.amount`) still fail", not "does the case fail"; other regressions' failures count as pass. Weighted bisect, per-repeat cache and median budget checks followed (BUILD_LOG "Post-dogfood fixes").

## Run 3 (re-run after the fix, bad end 0ecffc0)
- Report: `rerun3-summary.md`, `rerun3-smart/`, `rerun3-compare/`.
- Same signature `call_args:issue_refund.amount` now named **b0be626 (R3)**, revert test passed, pass rate 0% to 100%. Plain 24 vs smart 18 rounds.
- Behavioural `must_not_call` (cancel_order) named `0ecffc0`, revert passed, 4 vs 3 rounds.
- Overall 4 of 6 signatures correct against the private key.

## What did not improve (stated plainly)
- Token-budget signatures: smart is still slower than plain (60 vs 54, 96 vs 84 rounds) and two of three budget culprits were wrong because R1 and R2 both inflate every category. A tool-count signal is being added for this.
- Run 1 and run 3 used different ranges and signatures, so 84 vs 36 and the run 3 figures are not a like-for-like speed comparison. The like-for-like claim is the culprit: wrong then, right now, for the refund-amount signature.
