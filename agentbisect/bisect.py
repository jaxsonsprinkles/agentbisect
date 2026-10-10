"""Bisect engine: resolve + confirm the range, then let a Strategy pick commits until one is confirmed.

PlainStrategy is the baseline (binary search over first-parent history, git-bisect-skip style neighbour picking).
Chunk 7's smart planner plugs in by implementing `Strategy`. Every step is bracketed by begin_step/end_step so a
crashed run resumes via `resume_investigation`; finished case runs live in the verdict cache, so a redone step
only repeats the in-flight cases.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import BisectState, CommitResult, CulpritReport, Plan
from .runner import Runner, RunnerConfig
from .state import StateError, StateStore
from .tracing import step_span

COST_PER_RUN_USD = float(os.environ.get("AGENTBISECT_COST_PER_RUN", "0.002"))  # rough estimate per eval run
MULTI_REGRESSION_WARNING = "multiple regressions likely: verdicts are non-monotonic (a good commit after a bad one)"
EXIT_OK, EXIT_ERROR, EXIT_UNRESOLVED = 0, 1, 4


class BisectError(RuntimeError):
    pass


class Strategy(Protocol):
    def next_commit(self, state: BisectState) -> str | None:
        """Next sha to evaluate, or None when the search is finished."""

    def update(self, state: BisectState, sha: str, result: CommitResult) -> None:
        """Observe a verdict (state.results already contains it)."""


# -- git helpers --

def _git(repo: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise BisectError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def resolve(repo: str, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")


def linear_history(repo: str, good: str, bad: str) -> list[str]:
    """[good, c1, ..., bad]: first-parent chain, oldest first. Fails if good is not on bad's first-parent line."""
    chain = _git(repo, "rev-list", "--first-parent", "--reverse", f"{good}..{bad}").split()
    if not chain or chain[-1] != bad:
        raise BisectError(f"good {good[:8]} is not on the first-parent history of bad {bad[:8]}")
    return [good, *chain]


# -- narrowing (pure function of history + results; resume-safe) --

@dataclass
class Narrowing:
    lo: int  # newest known-good index below hi
    hi: int  # oldest known-bad index
    untested: list[int] = field(default_factory=list)  # indexes strictly between lo and hi with no verdict yet
    inconclusive: list[int] = field(default_factory=list)  # strictly between: skip / flaky
    non_monotonic: bool = False

    @property
    def done(self) -> bool:
        return not self.untested

    @property
    def culprit(self) -> int | None:
        """Index of the culprit when the search converged on a single commit."""
        return self.hi if self.done and self.hi - self.lo == 1 else None


def narrow(history: list[str], results: dict[str, CommitResult]) -> Narrowing:
    def v(i: int) -> str | None:
        r = results.get(history[i])
        return r.verdict if r else None

    bad = [i for i in range(len(history)) if v(i) == "bad"]
    good = [i for i in range(len(history)) if v(i) == "good"]
    hi = min(bad) if bad else len(history) - 1
    lo = max([i for i in good if i < hi], default=0)
    non_mono = any(g > b for b in bad for g in good)
    between = range(lo + 1, hi)
    return Narrowing(lo, hi, [i for i in between if v(i) is None],
                     [i for i in between if v(i) in ("skip", "flaky")], non_mono)


class PlainStrategy:
    """Binary search; if the midpoint is skip/flaky-inconclusive it is already in `results`, so the nearest
    untested neighbour is taken instead (alternating sides), like `git bisect skip`."""
    name = "plain"

    def __init__(self, history: list[str]):
        self.history = history

    def next_commit(self, state: BisectState) -> str | None:
        n = narrow(self.history, state.results)
        if not n.untested:
            return None
        mid = (n.lo + n.hi) / 2
        return self.history[min(n.untested, key=lambda i: (abs(i - mid), i))]

    def update(self, state: BisectState, sha: str, result: CommitResult) -> None:
        pass


# -- engine --

@dataclass
class Outcome:
    kind: str  # culprit | unresolved | failed
    sha: str | None = None
    candidates: list[str] = field(default_factory=list)
    message: str = ""


def make_runner(store: StateStore, st: BisectState) -> Runner:
    if not st.spec or not st.spec.cases:
        raise BisectError("investigation has no eval cases (run intake first)")
    return Runner(st.repo, RunnerConfig(cases=list(st.spec.cases)), store.cache(st.repo))


def _evaluate(store: StateStore, st: BisectState, runner: Runner, sha: str, kind: str) -> CommitResult:
    step = store.begin_step(st, kind, sha=sha)
    before = runner.eval_rounds
    with step_span("bisect_step", sha=sha, step=step, kind=kind):
        res = runner.evaluate_commit(sha, f"{st.id}-s{step}")
    m = st.metrics
    delta = runner.eval_rounds - before
    m["eval_rounds"] = m.get("eval_rounds", 0) + delta
    m["cost_usd"] = round(m["eval_rounds"] * COST_PER_RUN_USD, 4)
    m["commits_evaluated"] = m.get("commits_evaluated", 0) + (0 if res.from_cache else 1)
    m["cache_hits"] = m.get("cache_hits", 0) + (1 if res.from_cache else 0)
    store.end_step(st, sha, res)
    return res


def _fail(store: StateStore, st: BisectState, msg: str) -> Outcome:
    store.set_status(st, "failed", error=msg)
    return Outcome("failed", message=msg)


def _confirm_ends(store: StateStore, st: BisectState, runner: Runner, good: str, bad: str) -> str | None:
    """Re-run both ends once (cached verdicts count). Returns an error message or None."""
    for sha, want, label in ((bad, "bad", "bad end"), (good, "good", "good end")):
        res = st.results.get(sha)
        if res is None or res.verdict == "skip":
            res = _evaluate(store, st, runner, sha, f"confirm_{want}")
        if res.verdict != want:
            if want == "bad" and res.verdict == "good":
                return f"bad end does not reproduce: {sha[:8]} passes (pass_rate {res.pass_rate:.2f})"
            if want == "good" and res.verdict == "bad":
                return f"good end is not good: {sha[:8]} fails (pass_rate {res.pass_rate:.2f})"
            return f"{label} {sha[:8]} is unusable: verdict {res.verdict} ({res.reason})"
    return None


def _confidence(history: list[str], st: BisectState, n: Narrowing) -> tuple[str, dict]:
    lo_r, hi_r = st.results[history[n.lo]], st.results[history[n.hi]]
    band = {"good_pass_rate": lo_r.pass_rate, "bad_pass_rate": hi_r.pass_rate}
    inconclusive = [h for h in history if (r := st.results.get(h)) and r.verdict in ("skip", "flaky")]
    if n.non_monotonic:
        return "low", band
    if inconclusive or lo_r.pass_rate is None or hi_r.pass_rate is None:
        return "medium", band
    clear = lo_r.pass_rate >= 0.95 and hi_r.pass_rate <= 0.4
    return ("high" if clear else "medium"), band


def run_investigation(store: StateStore, st: BisectState, runner: Runner | None = None,
                      strategy: Strategy | None = None) -> Outcome:
    if st.status == "created":
        store.set_status(st, "running")
    elif st.status != "running":
        raise StateError(f"cannot run an investigation in status '{st.status}'")
    try:
        runner = runner or make_runner(store, st)
        bad = resolve(st.repo, st.bad_sha or "HEAD")
        if not st.good_sha:
            return _fail(store, st, "no good commit known: pass --good <sha>")
        good = resolve(st.repo, st.good_sha)
        st.good_sha, st.bad_sha = good, bad
        if good == bad:
            return _fail(store, st, "good and bad are the same commit")
        history = linear_history(st.repo, good, bad)
        if st.plan is None:
            st.plan = Plan(mode="plain")
        st.metrics.setdefault("strategy", getattr(strategy, "name", "plain"))
        strategy = strategy or PlainStrategy(history)
        store.save(st)

        err = _confirm_ends(store, st, runner, good, bad)
        if err:
            return _fail(store, st, err)
        while True:
            sha = strategy.next_commit(st)
            if sha is None:
                break
            strategy.update(st, sha, _evaluate(store, st, runner, sha, "bisect"))
        _monotonic_probe(store, st, runner, history)
        return _conclude(store, st, history)
    except (BisectError, StateError) as e:
        return _fail(store, st, str(e))
    except Exception as e:  # infra/pin errors: keep in_flight so `resume` redoes the step
        st.status, st.error = "failed", f"{type(e).__name__}: {e}"
        store.save(st)
        return Outcome("failed", message=st.error)


def _monotonic_probe(store: StateStore, st: BisectState, runner: Runner, history: list[str]) -> None:
    """One extra evaluation between the culprit and the bad end: if it is good, the regression is not a clean step
    (good after bad), which _conclude flags as multiple regressions. Skipped when no commit lies between."""
    n = narrow(history, st.results)
    if n.culprit is None:
        return
    i = (n.hi + len(history) - 1) // 2
    if n.hi < i < len(history) - 1 and history[i] not in st.results:
        _evaluate(store, st, runner, history[i], "monotonic_probe")


def _conclude(store: StateStore, st: BisectState, history: list[str]) -> Outcome:
    n = narrow(history, st.results)
    warnings = st.metrics.setdefault("warnings", [])
    if n.non_monotonic and MULTI_REGRESSION_WARNING not in warnings:
        warnings.append(MULTI_REGRESSION_WARNING)
    if n.culprit is None:
        cands = [history[i] for i in range(n.lo + 1, n.hi + 1)]
        st.metrics["ambiguous"] = cands
        msg = (f"culprit is within these skipped/flaky commits (cannot be narrowed): "
               f"{', '.join(c[:8] for c in cands)}")
        st.status, st.error = "failed", msg  # needs a human; resume allowed
        store.save(st)
        return Outcome("unresolved", candidates=cands, message=msg)
    conf, band = _confidence(history, st, n)
    st.metrics["confidence_band"] = band
    st.metrics.pop("ambiguous", None)
    sha = history[n.hi]
    if not (st.culprit and st.culprit.sha == sha):  # a resumed run keeps its analysis / revert test
        st.culprit = CulpritReport(sha=sha, confidence=conf)
    store.set_status(st, "culprit_found")
    return Outcome("culprit", sha=sha, message=f"culprit {sha[:8]} (confidence {conf})")


def report(st: BisectState, out: Outcome) -> str:
    m = st.metrics
    lines = [f"[{st.id}] {out.message}" if out.message else f"[{st.id}] {out.kind}"]
    for w in m.get("warnings", []):
        lines.append(f"  WARNING: {w}")
    lines.append(f"  strategy={m.get('strategy', 'plain')} steps={st.step} eval_rounds={m.get('eval_rounds', 0)} "
                 f"cost~${m.get('cost_usd', 0.0)} cache_hits={m.get('cache_hits', 0)}")
    return "\n".join(lines)


def resume_investigation(store: StateStore, st: BisectState, runner: Runner | None = None,
                         strategy: Strategy | None = None) -> int:
    """Entry point used by `agentbisect resume`; also the tail of `investigate --strategy`. Exit code."""
    out = run_investigation(store, st, runner, strategy)
    print(report(st, out))
    return {"culprit": EXIT_OK, "unresolved": EXIT_UNRESOLVED}.get(out.kind, EXIT_ERROR)
