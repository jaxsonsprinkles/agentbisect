# Roles, pinned models, isolation

Five separate roles. They must not share context.

| # | Role | What it is | Model |
| - | ---- | ---------- | ----- |
| 1 | History builder | A Claude Code session with `entire` on that writes support-agent-demo's ~20 commits (the seeded regressions). | its own session model |
| 2 | Support agent | The runtime lab rat, exercised by every eval. | PINNED Haiku: `AGENT_MODEL`, default `claude-haiku-5-5` |
| 3 | Eval judge | Grades replies against a rubric, only after deterministic checks pass and only when a rubric exists. | PINNED, smarter than the agent: `JUDGE_MODEL`, default `claude-sonnet-5-5` |
| 4 | AgentBisect | The tool: bisects the history using eval results. (Not built yet.) | n/a |
| 5 | Fixer | A fresh agent that repairs the culprit commit. | its own session model |

## Pinning rules (implemented in support-agent-demo `evals/models.py`, `evals/judge.py`, `evals/harness.py`)

- Tier order: haiku < sonnet < opus/fable. The judge must be a different model than the agent, not haiku-tier,
  and a higher tier than the agent. Violations raise `ModelConfigError` (CLI exits non-zero, `LLMJudge` refuses
  to construct).
- Judge: temperature 0, strict rubric, structured JSON verdict (per-criterion pass/fail + reason), overall
  `passed` computed in code as all-criteria-pass, `rubric_sha256` and `judge_model` recorded in each verdict.
- `agent_model` and `judge_model` are written into every eval report, every case result and every baseline.
  The harness refuses to run against a baseline, and `compare_reports` refuses to compare reports, whose pinned
  models differ or are missing, so a model cannot silently drift between commits. Changing a pin means
  recalibrating the baseline and re-running, never mixing.
- Tests use fake providers only; no live Claude call has been made (no API key at build time).

## Isolation rules

- History builder and AgentBisect builder are separate sessions that know nothing about each other. The history
  builder never sees AgentBisect, the eval ground truth, or this repo.
- The Fixer is fresh and knows nothing about how bugs were planted. Its context contains ONLY: the culprit diff,
  the intent of that change, and the failing eval evidence. It never receives the history-builder briefs, the
  planted-regression names (R1/R2/R3), or any `breaks`/`intent_for` metadata.
- Ground truth is kept out of the target repo. `breaks` (R1/R2/R3/control) and `intent_for` were removed from
  support-agent-demo `evals/cases.jsonl` (the loader rejects them) and live in
  `evidence/ground_truth.json` in this repo. It is for scoring AgentBisect's answer, and must be stripped from
  anything handed to the support agent, judge or Fixer. Evidence passed to the Fixer must be built from eval
  results (which carry no `breaks`), not from this file.
