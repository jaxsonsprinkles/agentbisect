# Live Fixer -> Verifier runs on the R3 refund-amount culprit (b0be626)

`scripts/demo_fix_verify.sh`, throwaway clone of the demo repo, DEMO_FLAKE=0, calibrated baseline, pinned judge claude-sonnet-5-5. Logs include raw proxy stats; no rate-limit events.

| run | what happened |
|---|---|
| 1 `run1-revert-caught.log` | Fixer attempt 3 passed its checks by reverting the culprit (dollars schema). Verifier: target and regression pass, intent judge FAILS ("reverts the culprit's intent"); held-out 0.25, mostly unrelated budget flags. Led to: intent check inside the Fixer; budget flags of other regressions ignored in regression/held-out. |
| 2 `run2-intent-kept-evals-frozen.log` | With the intent check, the Fixer kept integer cents and changed only the error message; 3/3 attempts failed. Eval cases expect amount 64.0 while the culprit deliberately made the tool take cents: no code change satisfies both. Led to: guarded eval-expectation edits. |
| 3 `run3-eval-edit.log` | Attempt 1 changed rw1-rw3 call_args expectations to cents (flagged for human review). Verifier: target PASS, regression PASS (13/13), intent PASS, held-out FAIL: rw4 (held-out, same contract) still expects 36.0. Overall NOT VERIFIED. |

Honest status: no fully verified fix yet. The remaining blocker is that the Fixer may not edit held-out cases, so a contract change leaves the same stale expectation in held-out cases.
