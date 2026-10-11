"""Token baseline: per-case median tokens measured at the good commit, and the budget rule built on it.

File format (a superset of the target harness's `baseline.json`, so the same file is also passed to the harness via
`--baseline`): {schema_version, commit, good_sha, agent_model, judge_model, repeats, created_at, date, token_factor,
max_steps, cases: {id: {median_tokens, median_steps, n}}}.

BUDGET RULE (used by the runner, the bisect and the revert test):
  * a budget signature (`budget:tokens...` / `budget:steps...`) runs every one of its cases `budget_repeats` times
    (AGENTBISECT_BUDGET_REPEATS, default 3) per commit; behavioural signatures stay at `repeats` (default 1);
  * per case, the commit's metric is the MEDIAN over those repeats; the case is over budget when that median
    exceeds the limit (tokens: `token_factor` x the case's baseline median; steps: `max_steps`);
  * a TOOL-REFINED budget signature (`budget:tokens@<category>+tool:<name>`, made at intake when a token budget fires
    and the baseline (version 2) shows which tool's calls rose) is decided by call counts, not tokens: per case the
    MEDIAN over the repeats of the tool's calls per run vs the baseline's per-case median; the case is over when it
    exceeds it by >= TOOL_EXCESS (1) call. Same budget_repeats runs, nothing extra, no noisy token totals;
  * a commit's pass rate for the signature is the fraction of its cases within budget (a case is weighted by its
    executed runs, i.e. equally unless a repeat errored), and the usual confidence band (good >= 0.85, bad <= 0.5) turns it into good / bad / flaky.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TOKEN_FACTOR = 1.5
BASELINE_VERSION = 2  # 2 = per-case per-tool call counts (`tool_counts`) next to the token medians
TOOL_EXCESS = 1  # a case is over for a tool when its median call count exceeds the baseline's by >= this many
TOOL_SHARE = 0.5  # a tool-refined signature fires when at least this share of the signature's cases are over
_TOOL_SUFFIX = re.compile(r"\+tool:([\w.\-]+)$")


class BaselineError(RuntimeError):
    """The token baseline is missing, unreadable, or does not belong to this investigation."""


def budget_kind(key: str | None) -> str | None:
    """`budget:tokens@order_status` -> 'tokens'; None for any non-budget signature key."""
    m = re.match(r"budget:(\w+)", key or "")
    return m.group(1) if m else None


def tool_of(key: str | None) -> str | None:
    """`budget:tokens@order_status+tool:cancel_order` -> 'cancel_order'; None for any other key."""
    m = _TOOL_SUFFIX.search(key or "")
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

    def validate(self, good_sha: str | None, agent_model: str | None) -> "Baseline":
        """Refuse a baseline that was not measured at THIS investigation's good commit with the pinned agent model:
        a token budget against another commit's or model's medians would be meaningless."""
        where = self.path or "baseline"
        have = self.data.get("good_sha")
        if not have:
            raise BaselineError(f"{where} has no good_sha: it was not written by `agentbisect calibrate`")
        if not good_sha or not _same_sha(have, good_sha):
            raise BaselineError(f"{where} was calibrated at {have[:10]} but this investigation's good commit is "
                                f"{(good_sha or '?')[:10]}: run `agentbisect calibrate` at the good commit")
        if not self.data.get("agent_model") or self.data["agent_model"] != agent_model:
            raise BaselineError(f"{where} was calibrated with agent model {self.data.get('agent_model')!r} but the "
                                f"pinned agent model is {agent_model!r}")
        if not self.cases:
            raise BaselineError(f"{where} has no per-case medians")
        return self

    def median_tokens(self, case: str) -> float | None:
        c = self.cases.get(case)
        return float(c["median_tokens"]) if c and c.get("median_tokens") is not None else None

    @property
    def has_tool_counts(self) -> bool:
        return int(self.data.get("baseline_version") or 1) >= BASELINE_VERSION and any(
            "tool_counts" in c for c in self.cases.values())

    def tool_median(self, case: str, tool: str) -> float | None:
        """Median calls of `tool` per run of `case` at the good commit (0 when never called); None if not measured."""
        tc = (self.cases.get(case) or {}).get("tool_counts")
        return None if tc is None else float(tc.get(tool, 0))

    def limit(self, kind: str, case: str) -> float | None:
        if kind == "tokens":
            m = self.median_tokens(case)
            return None if m is None else self.token_factor * m
        if kind == "steps":
            return None if self.max_steps is None else float(self.max_steps)
        return None


def _same_sha(a: str, b: str) -> bool:
    return min(len(a), len(b)) >= 7 and (a.startswith(b) or b.startswith(a))


def need_tool_counts_message(sig_key: str) -> str:
    return (f"signature {sig_key} compares tool call counts, but the baseline has none (baseline_version < "
            f"{BASELINE_VERSION}): re-run `agentbisect calibrate --repo R --good SHA` once to write a version "
            f"{BASELINE_VERSION} baseline")


def need_baseline_message(sig_key: str) -> str:
    return (f"budget signature {sig_key} needs a token baseline from the good commit: run `agentbisect calibrate "
            "--repo R --good SHA` once and point AGENTBISECT_BASELINE at the file it writes (never recalibrated "
            "automatically)")


def build_baseline(entries: dict[str, dict], good_sha: str, models: dict[str, str], repeats: int,
                   skipped: dict[str, str] | None = None) -> dict[str, Any]:
    """entries: case -> verdict-cache aggregate at the good commit. Medians use only the runs whose deterministic
    checks passed (a budget miss does not disqualify a run), like the target harness's own calibration."""
    cases: dict[str, dict] = {}
    caps: list[int] = []
    excluded = dict(skipped or {})
    for case, e in sorted(entries.items()):
        ok = [d for d in e.get("detail") or [] if d.get("status") in ("pass", "fail") and d.get("tokens") is not None
              and all(f.startswith("budget:") for f in d.get("fails") or [])]
        if not ok:
            excluded[case] = "no run passed its deterministic checks (or reported tokens)"
            continue
        steps = [d["steps"] for d in ok if d.get("steps") is not None]
        caps += [d["max_steps"] for d in ok if d.get("max_steps")]
        cases[case] = {"median_tokens": statistics.median(d["tokens"] for d in ok),
                       "median_steps": statistics.median(steps) if steps else None, "n": len(ok)}
        counted = [Counter(d["tool_counts"]) for d in ok if d.get("tool_counts") is not None]
        if counted:  # per tool: median calls per run (0 for runs that did not call it)
            tools = sorted({t for c in counted for t in c})
            cases[case]["tool_counts"] = {t: statistics.median(c[t] for c in counted) for t in tools}
    now = datetime.now(timezone.utc)
    return {"schema_version": 1, "commit": good_sha, "good_sha": good_sha, "agent_model": models.get("agent_model"),
            "judge_model": models.get("judge_model"), "repeats": repeats, "provider": "anthropic",
            "created_at": now.isoformat(timespec="seconds"), "date": now.date().isoformat(),
            "token_factor": DEFAULT_TOKEN_FACTOR, "max_steps": max(caps) if caps else None, "cases": cases,
            **({"baseline_version": BASELINE_VERSION} if any("tool_counts" in c for c in cases.values()) else {}),
            "excluded_cases": excluded}


def calibrate(runner: Any, good_sha: str, cases: list[str], out: str | Path, run_id: str = "calibrate") -> dict[str, Any]:
    """Run the good commit once (`budget_repeats` per case, no baseline passed to the harness), write the per-case
    median tokens to `out` and return the baseline dict. Never called implicitly."""
    cfg = runner.cfg
    saved = cfg.baseline, runner.signature
    cfg.baseline, runner.signature = None, None  # no baseline for the harness while measuring one
    try:  # budget_cases: every case at exactly budget_repeats, never escalated
        res = runner.evaluate_commit(good_sha, run_id, cases, budget_cases=cases)
    finally:
        cfg.baseline, runner.signature = saved
    if res.verdict == "skip" and not res.executed:
        raise BaselineError(f"cannot calibrate at {good_sha[:10]}: {res.reason}")
    entries, skipped = {}, {}
    for c in cases:
        e = runner.cache.get(good_sha, c, cfg.models) or {}
        if "skip" in e:
            skipped[c] = f"skipped by the target: {e['skip']}"
        elif e:
            entries[c] = e
        else:
            skipped[c] = "no executed runs (every repeat errored)"
    data = build_baseline(entries, good_sha, cfg.models, cfg.budget_repeats, skipped)
    if res.verdict != "good":
        data["warnings"] = [f"the good commit evaluated as {res.verdict} (pass rate {res.pass_rate}): is it really good?"]
    if not data["cases"]:
        raise BaselineError("no case produced a usable token measurement; nothing written")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(data, indent=2) + "\n")
    return data


def median_over_budget(values: list[float], limit: float | None) -> bool | None:
    """The budget rule for one case: median of the repeats vs the limit; None when it cannot be decided."""
    if not values or limit is None:
        return None
    return statistics.median(values) > limit
