# Live Fixer -> Verifier runs on the R3 refund-amount culprit (b0be626)

`scripts/demo_fix_verify.sh`, throwaway clone of the demo repo, DEMO_FLAKE=0, calibrated baseline, pinned judge claude-sonnet-5-5. Logs include raw proxy stats; no rate-limit events.

| run | what happened |
|---|---|
| 1 `run1-revert-caught.log` | Fixer attempt 3 passed its checks by reverting the culprit (dollars schema). Verifier: target and regression pass, intent judge FAILS ("reverts the culprit's intent"); held-out 0.25, mostly unrelated budget flags. Led to: intent check inside the Fixer; budget flags of other regressions ignored in regression/held-out. |
| 2 `run2-intent-kept-evals-frozen.log` | With the intent check, the Fixer kept integer cents and changed only the error message; 3/3 attempts failed. Eval cases expect amount 64.0 while the culprit deliberately made the tool take cents: no code change satisfies both. Led to: guarded eval-expectation edits. |
| 3 `run3-eval-edit.log` | Attempt 1 changed rw1-rw3 call_args expectations to cents (flagged for human review). Verifier: target PASS, regression PASS (13/13), intent PASS, held-out FAIL: rw4 (held-out, same contract) still expects 36.0. Overall NOT VERIFIED. |

| 4 `run4-no-heldout-edit.log` | Same as run 3: Fixer passed its own (target-only) checks on attempt 1 and stopped; stale held-out rw4 failed in the Verifier. Led to: Fixer checker also runs held-out cases sharing the target's check key. |
| 5 `run5-contract-rule-bug.log` | Stale rw4 now fails the attempt, but the model's correct rw4 edit was refused: the 'same contract' rule only understood object-valued checks, not call_args lists (a bug in our guard, found live). Noisy budget/es3 lines also misled attempt 3. Led to: contract rule compares tool + changed argument names; failure text limited to the shared check key. |
| 6 `run6-verified.log` | Attempt 1 changed rw1-rw3 and held-out rw4 call_args expectations to integer cents. Verifier: VERIFIED: target PASS, regression PASS (13/13), held-out PASS 1.00 (with 1 held-out expectation change), intent PASS. |

Honest status: run 6 is VERIFIED, but note what the fix is: it changes no code. The culprit's contract change (integer cents) was deliberate and its eval expectations were stale, so the fix updates 4 eval expectations, all listed for human review (rw1-rw3 target, rw4 held-out). A reviewer should confirm that is the intended resolution. Six live runs, ~$0.9 of model calls total at list price, no rate-limit events. One run per configuration, so single-sample evidence.
