# Entire graph context savings (measured)

Measured on the first real investigation `6857f0b738` (support-agent-demo, range `2f49adb535`..`f7d82b789e`,
13 commits in range, 13 ranked candidates). Source: `evidence/report-6857f0b738.md` ("Steps and context tokens")
and the saved state `.agentbisect/investigations/6857f0b738.json` (`context_tokens_per_step`, `metrics.planner.context_tokens`);
the `compare-smart` run (`28eee54e3b`) recorded identical numbers.

| context the planner needed | tokens |
|---|---|
| With entire graph + checkpoints (`plan:graph:rank`) | 12,869 |
| Without the graph, reading the raw range (`plan:without_graph`) | 26,420 |
| Saved | 13,551 tokens, **51.3% cut** (12,869 / 26,420 = 0.487) |

## How it was measured

- Tokens are bytes/4 rounded up (`planner.tokens_of_bytes`), not model-reported counts, so compare only within this metric.
- "With graph": every byte the planner actually read while planning, recorded by `Planner._read(...)`: `entire graph impact`
  output to locate impacted code, then the checkpoint intent (or commit message when no checkpoint) of only the commits
  that the graph linked to that code. Logged per phase via `StateStore.log_context_tokens` (label `plan:graph:<phase>`).
- "Without graph" (`Planner._baseline`): what a reader with no graph would have to read to rank the same range: the full
  `git diff good..bad` in bytes, plus the intent text of every commit in range (those actually read counted as-is, the
  rest at the average size of those). Logged as `plan:without_graph`.
- Both numbers are produced in the same planning pass on the same range, so the ratio is like for like.

## Caveats

- One investigation, one range; the saving depends on how much of the range the graph can exclude.
- The "without graph" side is an estimate of the raw diff plus intents, not a second run that read it.
- This is planner context only. It is separate from eval rounds: on this run smart bisect used more eval rounds than plain
  (84 vs 36), which unit U3 addresses.
