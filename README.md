# AgentBisect

`git bisect` for agent behavior. Give it one bad neatlogs trace; it finds the commit that caused the regression, explains why that commit was written (Entire checkpoints), and ships a verified fix.

Built for neatHack (Oct 10-12, 2026). Status: scaffold. See `hackathon.md` for the build log.

Setup: `uv sync`, then copy `.env.example` to `.env` and fill in values.
