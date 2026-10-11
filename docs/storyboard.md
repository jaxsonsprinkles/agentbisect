# AgentBisect: demo storyboard

Six frames matching [`demo-script.md`](demo-script.md). Terminal frames: dark theme, large font. Evidence frames: screenshots of the real files (open HTML reports with `open evidence/report-<id>.html`). Render every frame from a real command run, never mocked output, and check each on-screen number against `evidence/`.

| # | Time | Frame | Source on screen | Notes |
|---|---|---|---|---|
| 1 | 0:00-0:20 | Problem | `git log --oneline` of `support-agent-demo` beside a red eval report | Highlight several failing case ids. |
| 2 | 0:20-0:55 | Signatures and the refusal | `agentbisect investigate ... --all-signatures` output; the results table in `evidence/rerun3-summary.md` | Zoom on signature keys. Highlight the es2 row ("none: known pre-existing failure, correctly refused"). Caption: 4 of 6 correct. |
| 3 | 0:55-1:40 | Smart vs plain | Planner candidate table with an intent snippet; `evidence/entire-context-savings.md` (12,869 vs 26,420 tokens, investigation `6857f0b738`, a different range); rounds column of the rerun3 table | Show both outcomes: smart wins on behavioural (18 vs 24, 3 vs 4), loses on budget (96 vs 84, 60 vs 54), ties on one (21 vs 21). Do not hide the losses. |
| 4 | 1:40-2:10 | Divergence fixture | `uv run python -m agentbisect.fixtures fixtures/divergence/os1-search-help` | Keep the `FIXTURE: ... Not live neatlogs data.` line visible. Two columns: good = `lookup_order`; bad = `lookup_order`, then `search_help` x3 (two time out). Mark step 1. Caption: "illustrates the signal; not used in the final run (neatlogs dlq)". |
| 5 | 2:10-2:45 | Proof | An `evidence/report-*.html` for a passed signature (revert test 0% -> 100%) and one failed budget signature | Revert tests: 2 passed, 2 failed, 1 inconclusive. Show the quoted Entire checkpoint and the confidence label. |
| 6 | 2:45-3:00 | Close | `assets/logo.png` + "Find the commit. Know why it was written. Prove it." | Fixer is next, not shipped. |
