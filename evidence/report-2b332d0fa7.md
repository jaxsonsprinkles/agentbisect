# AgentBisect report: ca150c73c6

Investigation `2b332d0fa7` - status `culprit_found` - range `1b40b27761`..`f7d82b789e`

**Confidence: low**

> **Warning: multiple regressions likely.** Verdicts conflict or the revert did not clear the regression, so this commit may be only one of several causes.

Flags: divergence unavailable: IntakeError: neatlogs CLI failed (TRACE_FINALIZATION_FAILED): {"code":"TRACE_FINALIZATION_FAILED","detail":"The trace could not be finalized for span projection.","instance":"/api/v1/public/traces/e2dad5356ed1bc49c59bc0446442df54/spans","requestId":"req_d6b26d0070ab2b564e2529603ebb0e29"}

## Failure signature

Bisected on the failure signature `check=budget(tokens@escalation)` (key `budget:tokens@escalation`; cases es1). A commit is bad only if THIS check fails; other failures count as a pass for this signature. AgentBisect separates overlapping regressions by failure signature.

## Culprit commit

`ca150c73c62dc5946ca7b64a7de53fdd7f49792c`

Entire checkpoint: `01M4KJK6EVYNM9SCKB1ETM0ZNJ`

## Original intent (from the Entire checkpoint of the commit; the coding agent's own prompt and reasoning)

```text
Prompt: Cap the customer message length at 2000 characters; longer input should be truncated with a note in the log. Add a test.

Reasoning:
Now the implementation. I'll log the truncation at WARNING (so it shows at the default level, unlike INFO tool-call logs) and log only lengths, not the message text.
Also update the module docstring in logs.py, which says logs are off unless INFO:
Customer messages are now capped at 2000 characters. All 212 tests pass and the change is committed as `ca150c7`. I haven't pushed it.
```

## Semantic diff and impacted symbols

```text
agent/logs.py: added function log_message_truncated (1 dependents)
agent/loop.py: body_changed function run_agent (16 dependents)
tests/test_agent.py: added function test_message_at_or_under_cap_passes_through_unlogged
tests/test_agent.py: added function test_long_message_truncated_with_warning_log
tests/test_agent.py: added function test_truncation_log_silenced_above_warning
```

Impacted symbols: `log_message_truncated`, `run_agent`, `main`, `run_case`, `__main__.py`, `run_suite`

## Trace divergence (good vs bad, span by span)

- trace divergence unavailable: IntakeError: neatlogs CLI failed (TRACE_FINALIZATION_FAILED): {"code":"TRACE_FINALIZATION_FAILED","detail":"The trace could not be finalized for span projection.","instance":"/api/v1/public/traces/e2dad5356ed1bc49c59bc0446442df54/spans","requestId":"req_9389fd814838bb259ccd1aa0ec5efe0d"}

## Revert test

Result: **FAILED: the regression remains after reverting**

Pass rate: good commit 100%, culprit 0%, HEAD with culprit reverted 0%.

## Smart vs plain bisect

| strategy | eval rounds | cost (USD, estimate) |
|---|---|---|
| smart | 8 | 0.016 |

## Fix

Fix: not attempted.

## Steps and context tokens

| step | label | context tokens |
|---|---|---|
| 3 | plan:graph:rank | 22305 |
| 3 | plan:without_graph | 39405 |

## Commits tested

| commit | verdict | pass rate | repeats | runs |
|---|---|---|---|---|
| `1b40b27761` | good | 100% | 1 | 1 |
| `2f49adb535` | good | 100% | 1 | 1 |
| `59c8b10ccb` | good | 100% | 1 | 1 |
| `96de44a0d6` | good | 100% | 1 | 1 |
| `b0be626692` | bad | 0% | 1 | 1 |
| `ca150c73c6` | bad | 0% | 1 | 1 |
| `d80dad7f9e` | bad | 0% | 1 | 1 |
| `f7d82b789e` | bad | 0% | 1 | 2 |

## Links and evidence

neatlogs traces (the dashboard URL pattern is a best guess; the id is always the source of truth):

- failing trace: `e2dad5356ed1bc49c59bc0446442df54` - <https://app.neatlogs.com/traces/e2dad5356ed1bc49c59bc0446442df54>
- passing trace (earlier): `855691942b5144da260f941fc5e8fe2d` - <https://app.neatlogs.com/traces/855691942b5144da260f941fc5e8fe2d>
- passing trace (earlier): `93be04533b9284ec8561a2cc7fb72f9b` - <https://app.neatlogs.com/traces/93be04533b9284ec8561a2cc7fb72f9b>
- passing trace (earlier): `05af5988478065fe8545587031483447` - <https://app.neatlogs.com/traces/05af5988478065fe8545587031483447>
- passing trace (earlier): `ac3850ede470778bcc698a9103855569` - <https://app.neatlogs.com/traces/ac3850ede470778bcc698a9103855569>
- passing trace (earlier): `abfb755cf363fe6f57a023b13a9b7865` - <https://app.neatlogs.com/traces/abfb755cf363fe6f57a023b13a9b7865>
- eval run at culprit: `local-218d5796362241f9` - <https://app.neatlogs.com/traces/local-218d5796362241f9>

Entire checkpoints (retrieve with the command, run in the target repo):

- culprit `01M4KJK6EVYNM9SCKB1ETM0ZNJ`: `entire checkpoint explain ca150c73c62dc5946ca7b64a7de53fdd7f49792c --full`
