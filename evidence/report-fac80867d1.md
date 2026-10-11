# AgentBisect report: 22f7fbd223

Investigation `fac80867d1` - status `culprit_found` - range `1b40b27761`..`f7d82b789e`

**Confidence: low**

> **Warning: multiple regressions likely.** Verdicts conflict or the revert did not clear the regression, so this commit may be only one of several causes.

Flags: divergence unavailable: IntakeError: neatlogs CLI failed (TRACE_FINALIZATION_FAILED): {"code":"TRACE_FINALIZATION_FAILED","detail":"The trace could not be finalized for span projection.","instance":"/api/v1/public/traces/e2dad5356ed1bc49c59bc0446442df54/spans","requestId":"req_9cb7fb7fd7aea7543cbf2dad41dd8a86"}

## Failure signature

Bisected on the failure signature `check=budget(tokens@multi_step)` (key `budget:tokens@multi_step`; cases ms1). A commit is bad only if THIS check fails; other failures count as a pass for this signature. AgentBisect separates overlapping regressions by failure signature.

## Culprit commit

`22f7fbd223555ae4d10eb2e37e1cd7b7ffa71962`

Entire checkpoint: `01M4KJMSTF1SNY8K8WCRNM5K3M`

## Original intent (from the Entire checkpoint of the commit; the coding agent's own prompt and reasoning)

```text
Prompt: Include an estimated delivery date for shipped orders in the lookup_order result, computed from the ship date (ship date + 5 days). Existing fields must stay as they are.

Reasoning:
Now the implementation: return a copy of each order with the computed field, so the stored order (used by refund/cancel) isn't mutated.
I'll use the Edit tool instead.
`lookup_order` now returns an estimated delivery date for shipped orders. All 214 tests pass and the work is committed as `22f7fbd`. It hasn't been pushed.
```

## Semantic diff and impacted symbols

```text
tests/test_agent.py: added function test_lookup_estimated_delivery_date_only_for_shipped_orders
tests/test_agent.py: added function test_lookup_keeps_existing_fields_and_does_not_mutate_stored_order
tools/impl.py: added function _with_delivery_estimate (2 dependents)
tools/impl.py: body_changed class ToolBox (30 dependents)
tools/impl.py: body_changed method ToolBox._tool_lookup_order (1 dependents)
```

Impacted symbols: `_with_delivery_estimate`, `ToolBox`, `ToolBox._tool_lookup_order`, `run_agent`, `run_case`, `main`, `run_suite`

## Trace divergence (good vs bad, span by span)

- trace divergence unavailable: IntakeError: neatlogs CLI failed (TRACE_FINALIZATION_FAILED): {"code":"TRACE_FINALIZATION_FAILED","detail":"The trace could not be finalized for span projection.","instance":"/api/v1/public/traces/e2dad5356ed1bc49c59bc0446442df54/spans","requestId":"req_d7c52db90ad1c8e49e8d387fdcc048e4"}

## Revert test

Result: **FAILED: the regression remains after reverting**

Pass rate: good commit 100%, culprit 0%, HEAD with culprit reverted 0%.

## Smart vs plain bisect

| strategy | eval rounds | cost (USD, estimate) |
|---|---|---|
| smart | 7 | 0.014 |

## Fix

Fix: not attempted.

## Steps and context tokens

| step | label | context tokens |
|---|---|---|
| 4 | plan:graph:rank | 22305 |
| 4 | plan:without_graph | 39405 |

## Commits tested

| commit | verdict | pass rate | repeats | runs |
|---|---|---|---|---|
| `1b40b27761` | good | 100% | 0 | 0 |
| `1cda36a34d` | bad | 0% | 1 | 1 |
| `22f7fbd223` | bad | 0% | 1 | 1 |
| `2f49adb535` | good | 100% | 1 | 1 |
| `875f4bd19a` | bad | 0% | 1 | 1 |
| `b0be626692` | bad | 0% | 1 | 1 |
| `ca150c73c6` | good | 100% | 1 | 1 |
| `f7d82b789e` | bad | 0% | 1 | 2 |

## Links and evidence

neatlogs traces (the dashboard URL pattern is a best guess; the id is always the source of truth):

- failing trace: `e2dad5356ed1bc49c59bc0446442df54` - <https://app.neatlogs.com/traces/e2dad5356ed1bc49c59bc0446442df54>
- passing trace (earlier): `855691942b5144da260f941fc5e8fe2d` - <https://app.neatlogs.com/traces/855691942b5144da260f941fc5e8fe2d>
- passing trace (earlier): `93be04533b9284ec8561a2cc7fb72f9b` - <https://app.neatlogs.com/traces/93be04533b9284ec8561a2cc7fb72f9b>
- passing trace (earlier): `05af5988478065fe8545587031483447` - <https://app.neatlogs.com/traces/05af5988478065fe8545587031483447>
- passing trace (earlier): `ac3850ede470778bcc698a9103855569` - <https://app.neatlogs.com/traces/ac3850ede470778bcc698a9103855569>
- passing trace (earlier): `abfb755cf363fe6f57a023b13a9b7865` - <https://app.neatlogs.com/traces/abfb755cf363fe6f57a023b13a9b7865>
- eval run at culprit: `local-83136cb4022e46a7` - <https://app.neatlogs.com/traces/local-83136cb4022e46a7>

Entire checkpoints (retrieve with the command, run in the target repo):

- culprit `01M4KJMSTF1SNY8K8WCRNM5K3M`: `entire checkpoint explain 22f7fbd223555ae4d10eb2e37e1cd7b7ffa71962 --full`
