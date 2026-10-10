# BUILD_LOG
See `hackathon.md` (chunk entries and "Resume notes"). Latest: chunk 4 complete; next chunk 5.

## Chunk 4: skeleton (models, state, runner, CLI, tracing)
- `agentbisect/models.py`: RegressionSpec, Plan(+Candidate), CommitResult, BisectState, CulpritReport, FixAttempt (JSON round-trip via `to_dict`/`from_dict`).
- `agentbisect/state.py`: `StateStore(.agentbisect/)` with atomic writes (tmp+fsync+rename), status transition table, `begin_step`/`end_step` (in-flight marker for crash resume), `log_context_tokens`, and `VerdictCache` keyed (repo, sha, case, agent_model, judge_model); build-failure skips cached too.
- `agentbisect/runner.py`: `Runner.evaluate_commit(sha, run_id)` -> `CommitResult`; detached git worktree (always cleaned), `uv sync` install (failure => `skip`), pluggable `EvalExecutor` (`SubprocessExecutor` runs `uv run python -m evals run --json --isolate --suite all --cases X --repeats N --commit SHA --run-id ID`, env `AGENTBISECT_*`), pinned-model enforcement (`ModelPinError`), bounded retry+backoff for rate limits/timeouts, parallel cases, confidence band (`good_min`/`bad_max`/`classify` hook) with repeat escalation to `max_repeats`, then verdict `flaky`.
- `agentbisect/cli.py`: `investigate`/`watch` stubs, `status`/`resume` work on state files. `agentbisect/tracing.py`: neatlogs workflow `agentbisect`, no-op without key.
- Tests: `uv run pytest` (21 pass; fake executors + throwaway git repo). No live model or neatlogs key was used; `SubprocessExecutor` is only tested against a stub `evals` package, not the real support-agent-demo.
- Eval secrets: worktrees lack gitignored `.env`; pass `RunnerConfig.env_file` or export keys in the environment.
