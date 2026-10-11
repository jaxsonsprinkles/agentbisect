# AgentBisect report: 1cda36a34d

Investigation `38d468aa35` - status `culprit_found` - range `1b40b27761`..`f7d82b789e`

**Confidence: low**

> **Warning: multiple regressions likely.** Verdicts conflict or the revert did not clear the regression, so this commit may be only one of several causes.

Flags: divergence unavailable: IntakeError: neatlogs CLI failed (TRACE_FINALIZATION_FAILED): {"code":"TRACE_FINALIZATION_FAILED","detail":"The trace could not be finalized for span projection.","instance":"/api/v1/public/traces/e2dad5356ed1bc49c59bc0446442df54/spans","requestId":"req_45c3842d7432ada4b1a64f0038b6171c"}

## Failure signature

Bisected on the failure signature `check=must_call` (key `must_call`; cases rw3). A commit is bad only if THIS check fails; other failures count as a pass for this signature. AgentBisect separates overlapping regressions by failure signature.

## Culprit commit

`1cda36a34d48d484524d692ac96688f9b98953c9`

Entire checkpoint: `01M4KJXM4GD7KBK5H77R3EMRM8`

## Original intent (from the Entire checkpoint of the commit; the coding agent's own prompt and reasoning)

```text
Prompt: Make escalation tickets more useful: the escalate_to_human summary should include the order id when the customer mentioned one. Update the tool description accordingly.

Reasoning:
The tool and prompt are updated. Next I'm adding eval checks and tests, then I'll run pytest.
All 252 pass. Quick check that the eval harness still passes with the reference agent, given the new case checks:
Escalation tickets now include the order id when the customer gave one. All 252 tests pass with `uv run pytest`, and the change is committed as `1cda36a` (not pushed).
```

## Semantic diff and impacted symbols

```text
agent/prompt.py: body_changed module agent/prompt.py
tests/test_agent.py: added function test_escalate_summary_includes_order_id
tests/test_agent.py: added function test_escalate_description_asks_for_order_id
tools/impl.py: body_changed class ToolBox (42 dependents)
tools/impl.py: signature_changed method ToolBox._tool_escalate_to_human (1 dependents)
```

Impacted symbols: `agent/prompt.py`, `ToolBox`, `ToolBox._tool_escalate_to_human`, `run_agent`, `run_case`, `main`, `run_suite`

## Trace divergence (good vs bad, span by span)

- trace divergence unavailable: IntakeError: neatlogs CLI failed (TRACE_FINALIZATION_FAILED): {"code":"TRACE_FINALIZATION_FAILED","detail":"The trace could not be finalized for span projection.","instance":"/api/v1/public/traces/e2dad5356ed1bc49c59bc0446442df54/spans","requestId":"req_743a661066b7322c0d423247bcf247ce"}

## Revert test

Result: **PASSED: the regression disappears when the culprit is reverted on HEAD**

Pass rate: good commit 100%, culprit 0%, HEAD with culprit reverted 100%.

## Smart vs plain bisect

| strategy | eval rounds | cost (USD, estimate) |
|---|---|---|
| smart | 1 | 0.002 |

## Fix

Fix: not attempted.

## Steps and context tokens

| step | label | context tokens |
|---|---|---|
| 3 | plan:graph:impact | 618 |
| 3 | plan:graph:fallback | 103 |
| 3 | plan:graph:rank | 18694 |
| 3 | plan:without_graph | 39948 |

## Commits tested

| commit | verdict | pass rate | repeats | runs |
|---|---|---|---|---|
| `1b40b27761` | good | 100% | 1 | 1 |
| `1cda36a34d` | bad | 0% | 1 | 1 |
| `5cee99b624` | good | 100% | 1 | 1 |
| `b0be626692` | good | 100% | 1 | 1 |
| `d80dad7f9e` | good | 100% | 1 | 1 |
| `f7d82b789e` | bad | 0% | 1 | 1 |
| `fd61e9c89a` | good | 100% | 1 | 1 |

## Links and evidence

neatlogs traces (the dashboard URL pattern is a best guess; the id is always the source of truth):

- failing trace: `e2dad5356ed1bc49c59bc0446442df54` - <https://app.neatlogs.com/traces/e2dad5356ed1bc49c59bc0446442df54>
- passing trace (earlier): `855691942b5144da260f941fc5e8fe2d` - <https://app.neatlogs.com/traces/855691942b5144da260f941fc5e8fe2d>
- passing trace (earlier): `93be04533b9284ec8561a2cc7fb72f9b` - <https://app.neatlogs.com/traces/93be04533b9284ec8561a2cc7fb72f9b>
- passing trace (earlier): `05af5988478065fe8545587031483447` - <https://app.neatlogs.com/traces/05af5988478065fe8545587031483447>
- passing trace (earlier): `ac3850ede470778bcc698a9103855569` - <https://app.neatlogs.com/traces/ac3850ede470778bcc698a9103855569>
- passing trace (earlier): `abfb755cf363fe6f57a023b13a9b7865` - <https://app.neatlogs.com/traces/abfb755cf363fe6f57a023b13a9b7865>
- eval run at culprit: `local-7a0fd2c68c494466` - <https://app.neatlogs.com/traces/local-7a0fd2c68c494466>

Entire checkpoints (retrieve with the command, run in the target repo):

- culprit `01M4KJXM4GD7KBK5H77R3EMRM8`: `entire checkpoint explain 1cda36a34d48d484524d692ac96688f9b98953c9 --full`
