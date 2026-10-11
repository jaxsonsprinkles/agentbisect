<p align="center"><img src="assets/logo.png" alt="AgentBisect" width="240"></p>

# AgentBisect

`git bisect` for agent behavior. Give it a failing agent run (or just a known-good and known-bad commit). It finds the commit that broke the behavior, explains why that commit was written, and proves the culprit by reverting it.

Built for neatHack (Oct 10, 2026).

> Every figure below is copied from a file in `evidence/` (final re-run: [`evidence/rerun3-summary.md`](evidence/rerun3-summary.md)).
> Open the HTML reports with `open evidence/report-<id>.html`; `evidence/dogfooding.md` covers using AgentBisect on its own build.

## What it is

Agents regress in ways unit tests do not catch: a refactor changes the unit of a refund amount, a "helpful" change makes the agent cancel orders nobody asked to cancel, a new tool schema quietly doubles token spend. `git bisect` can find the commit, but it needs a deterministic good/bad test, and an LLM agent gives you a noisy one.

AgentBisect supplies that test. It runs the target repo's eval suite at each commit it probes, in an isolated git worktree, with repeats and a calibrated baseline so a flaky run is not mistaken for a regression. Then it bisects.

The demo target is [`support-agent-demo`](https://github.com/jaxsonsprinkles/support-agent-demo): a customer-support agent (pinned Haiku) with mocked tools, 24 eval cases at HEAD (18 exist at the good commit), and a seeded history of regressions written by a separate coding-agent session.

## How it works

1. **Intake.** Start from a neatlogs trace, a neatlogs detection, or a GitHub issue. Intake maps it to an eval case, finds a known-good commit (an earlier passing run of the same case), and records a `RegressionSpec`.
2. **Failure signatures.** At the bad end, every distinct failing check (for example `call_args:issue_refund.amount`, `must_not_call`, `budget:tokens@order_status`) becomes its own *signature*, and each is bisected separately. One range can hide several regressions; bisecting "the suite fails" would blame whichever commit lands last. A signature that also fails at the good end is a known pre-existing failure, and AgentBisect refuses to bisect it instead of inventing a culprit.
3. **Plain bisect.** Classic binary search over `git rev-list --first-parent`, with a monotonic probe after convergence that flags "multiple regressions likely" when the history is not clean good-then-bad.
4. **Smart (weighted) bisect.** A planner ranks the commits in range by how likely each is to be the culprit, then picks the next commit by weighted bisection (split the probability mass, not the commit count) and falls back to plain when the ranking is uninformative. Signals, in order:
   - **Span divergence**: where the bad run first differs from a good run (a different tool, different arguments, an extra step). See [span divergence](#span-divergence-and-the-fixture-fallback).
   - **Code impact**: `entire graph impact` finds which commits touched the code behind the divergent tool.
   - **Entire intent**: each candidate's [Entire](https://entire.io) checkpoint (the prompt and reasoning of the coding session that wrote it). Commits that were *meant* to change the failing area rank higher.
5. **Median budget checks.** Token and step regressions are judged against a per-case **median** from a one-time calibration run at the good commit (`agentbisect calibrate`), never recalibrated mid-investigation. A run is over budget past 1.5x the case median. Medians, not means, so one slow sample does not move the line.
6. **Verifier (revert test).** The culprit is only reported with confidence if reverting it at the bad end flips the signature from failing to passing (before and after pass rates are recorded). Revert conflicts, partial reverts and unbuildable trees lower confidence or end as `inconclusive`.
7. **Explain.** The culprit report quotes the Entire checkpoint (why the commit was written), the semantic diff, and the first span divergence, so the answer is "this commit, written to do X, causes Y", not just a SHA.
8. **Fixer (upcoming).** A fresh agent that sees only the culprit diff, the stated intent, and the failing evidence (never the planted-regression metadata) proposes a fix that keeps the intent. **Not built yet.** `report --open-pr` is a dry run by default and needs `--confirm-open-pr` to push anything.

Roles, model pins and isolation rules are in [`ROLES.md`](ROLES.md). The agent and judge models are pinned and recorded in every result, and results produced under different pins are never mixed.

### Measured so far

| What | Result | Source |
|---|---|---|
| Planner context with the Entire graph vs reading the raw range | 12,869 vs 26,420 tokens (51.3% cut). Measured on investigation `6857f0b738`, a different range from the final re-run | [`evidence/entire-context-savings.md`](evidence/entire-context-savings.md) |
| Signatures correct on the full-range re-run (good `1b40b27`, bad `0ecffc0`) | 4 of 6 | [`evidence/rerun3-summary.md`](evidence/rerun3-summary.md) |
| Revert tests | 2 passed, 2 failed, 1 inconclusive; the sixth signature (es2) was correctly refused as a pre-existing failure, so none was run | same |
| Eval rounds, plain vs smart | 24 vs 18, 4 vs 3, 84 vs 96, 54 vs 60, 21 vs 21. Smart won on the two behavioural signatures and lost on budget signatures (84 vs 96, 54 vs 60), tied on one (21 vs 21) | same |

`agentbisect summary <state_dir> --format md` regenerates the per-signature table from saved state.

## Setup

Requirements: Python 3.11 to 3.13 (the neatlogs SDK does not support 3.14), [`uv`](https://docs.astral.sh/uv/), `git`, and for the full pipeline the `entire` CLI with the `graph` plugin.

```bash
uv sync
cp .env.example .env     # NEATLOGS_API_KEY, NEATLOGS_PROJECT_ID, ...
uv run pytest -q         # fake executors and throwaway git repos; no model or network calls
```

The target repo needs `evals/cases.jsonl` and the eval CLI contract (`python -m evals run --json --isolate ...`, schema version 1) described in the `support-agent-demo` repo. Clone it next to this one.

Optional pieces:

- **neatlogs CLI** (`npm i -g --prefix ~/.local/neatlogs-cli neatlogs-cli`) for live trace intake, after a one-time `auth login --device`. See `hackathon.md`.
- **Entire** (`entire enable` in the target repo; `entire plugin install graph`) for checkpoint intent and code impact. Without it the planner falls back to commit messages and plain bisect.
- **LLM backend.** Either set `ANTHROPIC_API_KEY`, or run on your own logged-in `claude` CLI with `--llm-proxy auto` (personal use, your own login only; see `ROLES.md`). The proxy cannot set temperature, so repeats absorb the extra noise.

## Usage

```bash
# 1. Calibrate the token baseline once, at the known-good commit
uv run agentbisect calibrate --repo ../support-agent-demo --good 1b40b27 --llm-proxy auto

# 2. Investigate from a bad trace (or --detection ID, or --issue URL)
uv run agentbisect investigate --trace <trace-id> --repo ../support-agent-demo \
    --strategy smart --all-signatures --all-categories --llm-proxy auto

# or skip neatlogs and name the range yourself
uv run agentbisect investigate --trace <trace-id> --repo ../support-agent-demo \
    --fixtures path/to/bundle.json --good 1b40b27 --bad 0ecffc0

uv run agentbisect status [ID]          # investigations and their steps
uv run agentbisect resume ID            # continue after an interruption
uv run agentbisect compare ID           # plain vs smart: eval rounds and cost
uv run agentbisect summary --format md  # per-signature results table
uv run agentbisect report ID            # Markdown + HTML report in evidence/
```

State lives in `.agentbisect/` (atomic writes, resumable mid-step). Run `uv run agentbisect --help` for all flags.

### Span divergence and the fixture fallback

Live span divergence reads multi-span traces from neatlogs. At the time of writing, those traces finalize as `dlq` with zero spans (see Limitations), so for the demo the same step runs on a **recorded fixture**:

```bash
uv run python -m agentbisect.fixtures fixtures/divergence/os1-search-help
```

```
FIXTURE: recorded from real eval-runner output (tool calls of one case at a good and a bad commit). Not live neatlogs data.
good local-3b41... (1b40b27...), bad local-5455... (0ecffc0...), case os1
first divergence: step 1: bad run made an extra step search_help
```

The fixture is the tool-call sequence of case `os1` ("Where is order 1042?") at `1b40b27` and `0ecffc0`, taken from real `python -m evals run --json` output (Haiku through the llm-proxy). It is always labelled as a fixture, never presented as live neatlogs data. The bad commit is the tip of the range, so the fixture shows where behavior changed (an extra `search_help` step), not which commit caused it. It contains tool-call spans only; model-call spans are not in the runner's report. `agentbisect.fixtures.FixtureDirSource` plugs into the same `TraceSource` interface as the live source, and `--bundle OUT.json` writes the single-file form that `--fixtures` accepts. `record_pair` records new fixtures from two eval reports.

## Limitations

Stated plainly, because the evidence says so:

- **neatlogs span divergence is blocked.** Multi-span traces dead-letter (`finalizationStatus: "dlq"`, `spansCount: 0`) even for a bare two-span script with no model calls; the SDK reports zero export failures. Reported with trace ids in [`evidence/neatlogs-dlq-report.md`](evidence/neatlogs-dlq-report.md). Because of this, the final re-run's planner ranked on code impact and checkpoint intent only, with no span divergence; the fixture above illustrates the signal and was not used in that run. The neatlogs read path (`NeatlogsCliSource.spans`) was written against the documented CLI and its real span shape is unverified.
- **Token-budget signatures are the weak spot.** Every token regression (a new tool schema, an extra "verify with search" rule) inflates every category, so a per-category budget signature tends to blame whichever commit crosses 1.5x last, and its revert test can fail with "multiple regressions likely". Behavioural signatures (wrong arguments, forbidden tool calls) are far more reliable. See `evidence/rerun3-summary.md` for the per-signature outcomes: 4 of 6 correct on the final re-run; the three token-budget signatures were the misses or mixed.
- **Smart is not always cheaper.** It used fewer eval rounds than plain on behavioural signatures (18 vs 24, 3 vs 4) but more on budget signatures (96 vs 84, 60 vs 54). The planner-context saving is measured on one investigation and one range.
- **Noisy judge of "bad".** Haiku at non-zero temperature (the proxy cannot set it) is stochastic. Repeats, medians and a confidence band reduce but do not remove this; low-confidence culprits are labelled as such.
- **Token counts for context savings are bytes/4 estimates**, comparable only within that metric, and the "without graph" side is an estimate, not a second run.
- **Fixer is not built.** Culprit explanation and the revert test exist; automatic fix generation and fix verification do not.
- **One target repo.** Everything was exercised against `support-agent-demo`. The eval contract is generic, but nothing else has been tried.

## Repository map

| Path | What |
|---|---|
| `agentbisect/intake.py` | trace/detection/issue intake, `TraceSource` (neatlogs CLI and fixture sources) |
| `agentbisect/runner.py`, `baseline.py` | isolated eval runs, repeats, caching, median-token baseline |
| `agentbisect/signature.py`, `bisect.py`, `planner.py` | failure signatures, plain and weighted bisect, ranking |
| `agentbisect/culprit.py`, `verify.py` | Entire intent + diff + divergence, revert test |
| `agentbisect/fixtures.py`, `fixtures/` | recorded good/bad trace pair for the divergence step |
| `agentbisect/llm_proxy.py` | Anthropic-compatible proxy over your `claude` login |
| `evidence/` | reports and re-run summaries |
| `docs/demo-script.md`, `docs/storyboard.md` | 3-minute demo narration and shot list |
| `hackathon.md`, `BUILD_LOG.md`, `ROLES.md` | build log, roles and pinning rules |
