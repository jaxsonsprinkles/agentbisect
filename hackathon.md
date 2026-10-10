# AgentBisect build log (neatHack)

## Chunk 0: setup + discovery (Oct 10, 2026)

Done: uv project, two public repos with Entire enabled, secrets in gitignored `.env`, discovery below.

Repos: https://github.com/jaxsonsprinkles/agentbisect , https://github.com/jaxsonsprinkles/support-agent-demo

### Environment notes
- `neatlogs` Python SDK requires Python >=3.10,<3.14, so the project is pinned `requires-python = ">=3.11,<3.14"` with `.python-version` = 3.13 (system python 3.14 does not work). Install: `uv sync` (uv installs 3.13 itself).
- Two different things are called "neatlogs CLI":
  1. SDK helper installed by pip/brew (`/opt/homebrew/bin/neatlogs`, v1.4.26): only `doctor --local|--probe` and `trace get <id>`.
  2. Public API CLI, npm `neatlogs-cli` 0.2.1, binary `neatlogs`. It collides with the brew binary, so it is installed with a prefix: `npm install --global --prefix ~/.local/neatlogs-cli neatlogs-cli@0.2.1`, run as `~/.local/neatlogs-cli/bin/neatlogs`. Node 22/24 documented (26 works).

### (a) Entire
- Enable (done in both repos): `entire enable --agent claude-code --yes` (commit the generated `.entire/` and `.claude/`).
- Read a commit's checkpoint (prompts, intent, files, tokens): `entire checkpoint explain <sha-or-checkpoint-id>`; `--short`, `--full` (parsed transcript), `--raw-transcript` / `--transcript` (JSONL), `--json` (metadata only, no transcript). Other repo: `--repo gh/owner/name` (needs full checkpoint id, pushed).
- List: `entire checkpoint list`; search: `entire checkpoint search "text"` / `entire search "text"`; tokens: `entire checkpoint tokens <id>`; session: `entire session info <session-id> --json`, `entire session list`; line provenance: `entire why <file:line>`.
- Commit-to-checkpoint link is the `Entire-Checkpoint:` trailer on the commit.
- entire-graph IS installed: plugin `graph` v0.4.0 at `~/.local/share/entire/plugins/bin` (via `entire plugin install graph` if missing). Invoke as `entire graph ...`:
  - `entire graph impact --symbol NAME|file:line --repo . --format json [--depth 1|2] [--head]`
  - `entire graph diff --base <rev> --head <rev> --json`
  - `entire graph commit <sha>` (entity changes vs first parent)
  - `entire graph checkpoint <checkpoint-id> --json`
  - `entire graph search "plain-language query"`, `entire graph verify`, `entire graph doctor`
  - Note: `entire graph diff/impact` need parsed languages; check `entire graph capabilities` for Python.

### (b) neatlogs tags / querying
- Tags are process-wide only: `neatlogs.init(api_key=..., workflow_name=..., tags=[...])`. No documented per-trace tag API. For per-run labelling use trace-root params: `with neatlogs.trace("name", session_id="<investigation-id>", end_user_id=..., end_user_metadata={...})` or `neatlogs.identify(session_id=..., session_custom_fields={"commit": sha, "case": id})`. Workflow name can be set per feature via attribute `neatlogs.workflow.name`.
- Practical plan: run each eval as a subprocess (own process) so `init(tags=[f"commit:{sha}", f"inv:{id}"], workflow_name=f"eval-{sha}")` works, and also record trace ids in `.agentbisect/` state.
- MCP `search_traces`: structured filters only `framework, status, min/max_latency_ms, min/max_cost, date_range`; free-text `query` matches names, errors, span names, metadata. NO tag filter. Other MCP tools: `get_trace_context(trace_id)`, `list_detections`, `get_detection_trend(detection_name, granularity, date_range)`, `log_trace`, `triage_*`.
- Public CLI `traces list` filters: `--status --workflow --session --from --to --limit --cursor --all --max-items`; also `traces search <q>`, `traces get`, `traces export --format json`, `traces spans list`, `traces payload <trace> <span> --fields`, `traces update-metadata <id> --body-stdin`. So filter by `--workflow eval-<sha>` or `--session <investigation-id>`; no tag filter flag found in the CLI (Public API `GET /api/v1/public/traces` params not verified).
- CLI auth WAITING ON A HUMAN: the SDK project key does not authenticate CLI reads. Run: `~/.local/neatlogs-cli/bin/neatlogs --host https://app.neatlogs.com --profile neatlogs-us --credential-source profile auth login --device` (approve the device code in browser), then `... --project 110108af-1737-47ee-8494-cf482d13defd whoami`. Note the keytar install script was skipped by npm; if keychain fails add `--allow-file-credentials`.

### (c) SDK
- Package `neatlogs` (1.4.26 installed via `uv add neatlogs`). Init: `neatlogs.init(api_key=os.environ["NEATLOGS_API_KEY"], workflow_name=..., tags=[...])`, `client = neatlogs.wrap(Client())`, end with `neatlogs.flush(); neatlogs.shutdown()`.
- `uv run python -m neatlogs doctor --local --json`: status pass (exit 0).
- `set -a; . ./.env; set +a; uv run python -m neatlogs doctor --probe --json`: synthetic probe passed all checks (visible, finalized, hierarchy, attributes, io, metadata, typed tokens). This verifies only the synthetic probe, not a real AgentBisect workflow.

## Resume notes
- Next: chunk 1 (build `support-agent-demo` agent + 24 evals, history with R1-R3 written via a coding agent with Entire on), then eval harness, intake, bisect, etc., per the plan.
- Setup: `cd agentbisect && uv sync`; `.env` is gitignored (copy `.env.example`; the real key is held by the user, also copied to `support-agent-demo/.env`).
- Always run `entire enable` hooks active (they are committed); no ANTHROPIC key yet, needed for the target agent.
- Open: Bedrock vs Anthropic API; neatlogs CLI device login.
