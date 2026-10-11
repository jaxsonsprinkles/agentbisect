# AgentBisect: 3-minute demo script

About 450 spoken words at 150 wpm. Visuals are in [`storyboard.md`](storyboard.md). All figures come from `evidence/` (final re-run: `evidence/rerun3-summary.md`). Keep a saved investigation ready; do not run a full bisect live.

## 0:00 - 0:20  Problem (about 50 words)

> You change an agent and something gets worse. A refund in the wrong units, an agent cancelling orders nobody asked it to cancel, a tool schema that doubles your token bill. Many commits, one red eval suite. Which commit did it?

## 0:20 - 0:55  Signatures, medians (about 85 words)

> AgentBisect is git bisect for agent behavior. Bad is noisy, so we split the failure into signatures, one per failing check, and bisect each on its own. Token budgets are judged against a median calibrated once at the good commit. Every commit runs in an isolated worktree with pinned models. On the final run, four of six signatures were right, and one, es2, was correctly refused: it already fails at the good commit, so there is nothing to bisect.

## 0:55 - 1:40  Smart bisect and Entire (about 100 words)

> Plain binary search spends an eval run per probe. The smart bisect ranks commits first, using the Entire graph for which commits touched the failing code, and Entire checkpoints for why each commit was written. The graph cut planner context from twenty-six thousand tokens to thirteen thousand. On the two behavioural signatures smart needed fewer eval rounds: eighteen against twenty-four, and three against four. On budget signatures it needed more: ninety-six against eighty-four, and sixty against fifty-four. Token regressions inflate everything, so budget signatures are our weak spot.

## 1:40 - 2:10  Divergence signal (about 70 words)

> One signal we want is where a bad run first diverges from a good one. This is a recorded fixture of real runner output, not live neatlogs, which currently dead-letters multi-span traces, so the final run did not use it. Same question, "Where is order 1042?": the bad run adds a search-help call at step one. That is the kind of divergence the planner can rank on.

## 2:10 - 2:45  Proof (about 75 words)

> We never report a SHA on faith. The verifier reverts the culprit and re-runs the signature. Two reverts passed, including the refund amount going from zero to one hundred percent. Two failed and one was inconclusive, all on budget signatures. The report quotes the checkpoint, so you read why the commit was written.

## 2:45 - 3:00  Next (about 40 words)

> Next is the Fixer: a fresh agent that sees only the culprit diff, the intent, and the failing evidence. Find the commit, know why it was written, prove it.
