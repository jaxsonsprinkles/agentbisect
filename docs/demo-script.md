# AgentBisect: 3-minute demo script

About 450 spoken words (about 390 below) at 150 wpm. Visuals are in [`storyboard.md`](storyboard.md). All figures come from `evidence/` (final re-run: `evidence/rerun3-summary.md`). Keep a saved investigation ready; do not run a full bisect live.

## 0:00 - 0:15  Problem (about 35 words)

> You change an agent and something gets worse. A refund in the wrong units, orders cancelled that nobody asked to cancel, a token bill that doubles. Many commits, one red eval suite. Which commit did it?

## 0:15 - 0:45  Signatures, medians (about 70 words)

> AgentBisect is git bisect for agent behavior. Bad is noisy, so we split the failure into signatures, one per failing check, and bisect each alone. Token budgets are judged against a median calibrated once at the good commit. On the final run, four of six signatures were right, and one, es2, was correctly refused: it already fails at the good commit.

## 0:45 - 1:20  Smart bisect and Entire (about 80 words)

> Plain binary search spends an eval run per probe. Smart bisect ranks commits first, using the Entire graph for which commits touched the failing code, and checkpoints for why each was written. The graph cut planner context from twenty-six thousand tokens to thirteen thousand. Smart needed fewer rounds on behavioural signatures, eighteen against twenty-four, but more on budget signatures, ninety-six against eighty-four. Budget signatures are our weak spot.

## 1:20 - 1:40  Divergence signal (about 45 words)

> This is a recorded fixture, not live neatlogs, which dead-letters multi-span traces. Same question, "Where is order 1042?": the bad run adds a search-help call at step one. That is a divergence the planner can rank on.

## 1:40 - 2:15  Proof (about 60 words)

> We never report a SHA on faith. The verifier reverts the culprit and re-runs the signature. Two reverts passed, including the refund amount going from zero to one hundred percent. Two failed and one was inconclusive, all budget signatures. The report quotes the checkpoint, so you read why the commit was written.

## 2:15 - 2:45  Fix beat (about 75 words)

> Then the Fixer. It sees only the culprit diff, the intent and the failing evidence. In an early run it just reverted the culprit; the intent judge caught that. Here the culprit deliberately moved refunds to cents, so the fix changes no code: it updates four stale eval expectations, all flagged for human review. Verified: target, thirteen of thirteen regression, held-out, intent. A reviewer still has to confirm it.

## 2:45 - 3:00  Close (about 25 words)

> Find the commit, know why it was written, prove it, and propose a fix a human can check. One culprit, six live runs.
