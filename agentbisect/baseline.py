"""Token baseline: per-case median tokens measured at the good commit, and the budget rule built on it.

File format (a superset of the target harness's `baseline.json`, so the same file is also passed to the harness via
`--baseline`): {schema_version, commit, good_sha, agent_model, judge_model, repeats, created_at, date, token_factor,
max_steps, cases: {id: {median_tokens, median_steps, n}}}.

BUDGET RULE (used by the runner, the bisect and the revert test):
  * a budget signature (`budget:tokens...` / `budget:steps...`) runs every one of its cases `budget_repeats` times
    (AGENTBISECT_BUDGET_REPEATS, default 3) per commit; behavioural signatures stay at `repeats` (default 1);
  * per case, the commit's metric is the MEDIAN over those repeats; the case is over budget when that median
    exceeds the limit (tokens: `token_factor` x the case's baseline median; steps: `max_steps`);
  * a commit's pass rate for the signature is the fraction of its cases within budget (a case is weighted by its
    executed runs, i.e. equally unless a repeat errored), and the usual confidence band (good >= 0.85, bad <= 0.5) turns it into good / bad / flaky.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

DEFAULT_TOKEN_FACTOR = 1.5


class BaselineError(RuntimeError):
    """The token baseline is missing, unreadable, or does not belong to this investigation."""


def budget_kind(key: str | None) -> str | None:
    """`budget:tokens@order_status` -> 'tokens'; None for any non-budget signature key."""
    m = re.match(r"budget:(\w+)", key or "")
    return m.group(1) if m else None


class Baseline:
    def __init__(self, data: dict[str, Any], path: str | None = None):
        self.data, self.path = data, path
        self.cases: dict[str, dict[str, Any]] = data.get("cases") or {}
        self.token_factor = float(data.get("token_factor") or DEFAULT_TOKEN_FACTOR)
        self.max_steps = int(data["max_steps"]) if data.get("max_steps") else None  # else the runs' own cap

    @classmethod
    def load(cls, path: str | Path) -> "Baseline":
        p = Path(path)
        try:
            return cls(json.loads(p.read_text()), str(p))
        except OSError as e:
            raise BaselineError(f"cannot read baseline {p}: {e}") from e
        except ValueError as e:
            raise BaselineError(f"baseline {p} is not valid JSON: {e}") from e

    def median_tokens(self, case: str) -> float | None:
        c = self.cases.get(case)
        return float(c["median_tokens"]) if c and c.get("median_tokens") is not None else None

    def limit(self, kind: str, case: str) -> float | None:
        if kind == "tokens":
            m = self.median_tokens(case)
            return None if m is None else self.token_factor * m
        if kind == "steps":
            return None if self.max_steps is None else float(self.max_steps)
        return None


def median_over_budget(values: list[float], limit: float | None) -> bool | None:
    """The budget rule for one case: median of the repeats vs the limit; None when it cannot be decided."""
    if not values or limit is None:
        return None
    return statistics.median(values) > limit
