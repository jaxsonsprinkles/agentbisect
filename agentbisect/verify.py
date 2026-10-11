"""Verifier. This chunk: the revert test that proves the culprit. (Fix verification hooks arrive in chunk 11.)

Revert test: in a fresh worktree at the investigated HEAD, `git revert --no-commit <culprit>`, snapshot the result as a
throwaway commit (so the Runner, cache and worktree machinery work unchanged), run the suite cases on it and require
the regression to disappear: pass rate back to the good-commit level within `tolerance`.

  * revert conflicts   -> abort, retry with only the culprit's hunks that still apply (`git apply -R --reject`),
                          flag "revert conflicts" and cap confidence. Nothing applicable -> inconclusive (None).
  * revert does not fix -> the culprit is probably wrong or there are several regressions: confidence low and the
                          "multiple regressions likely" flag.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bisect import COST_PER_RUN_USD
from .culprit import analyze_culprit
from .intake import TraceSource
from .models import BisectState, CommitResult, CulpritReport
from .runner import Runner, worktree
from .state import StateStore
from .tracing import step_span

REVERT_CONFLICTS = "revert conflicts"
MULTI_REGRESSION_FLAG = "multiple regressions likely: reverting the culprit did not remove the regression"
TOLERANCE = 0.1  # pass rate may sit this far below the good-commit level (the confidence band)
_LEVELS = ["low", "medium", "high"]
# fixed identity and dates make the throwaway commit's sha a pure function of (HEAD tree, culprit): resume-safe cache
_GIT_ENV = {"GIT_AUTHOR_NAME": "agentbisect", "GIT_AUTHOR_EMAIL": "agentbisect@localhost",
            "GIT_COMMITTER_NAME": "agentbisect", "GIT_COMMITTER_EMAIL": "agentbisect@localhost",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}


def budget_view(b: dict[str, Any] | None) -> dict[str, Any] | None:
    """The few numbers of a CommitResult.budget that the revert test reports: median metric (median over cases of
    the per-case medians over the repeats), baseline median, cases within budget / total, repeats."""
    if not b or not b.get("cases"):
        return None
    return {k: b.get(k) for k in ("kind", "median", "baseline_median", "ratio", "within", "total", "repeats")}


def downgrade(conf: str, steps: int = 1) -> str:
    i = _LEVELS.index(conf) if conf in _LEVELS else 0
    return _LEVELS[max(0, i - steps)]


@dataclass
class RevertBuild:
    sha: str | None  # throwaway commit holding HEAD with the culprit reverted; None when nothing could be reverted
    kind: str  # clean | partial | conflict | empty
    detail: str = ""
    rejected: int = 0


def _git(wt: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, input=stdin,
                          env={**os.environ, **_GIT_ENV})


def _reset(wt: Path) -> None:
    _git(wt, "revert", "--abort")
    _git(wt, "reset", "--hard", "HEAD", "-q")
    _git(wt, "clean", "-fdxq")


def _snapshot(wt: Path, culprit: str) -> str | None:
    """Commit whatever the worktree now holds on top of HEAD; None if the tree is unchanged."""
    _git(wt, "add", "-A")
    if _git(wt, "diff", "--cached", "--quiet").returncode == 0:
        return None
    p = _git(wt, "commit", "-q", "--no-verify", "-m", f"agentbisect revert test of {culprit}")
    if p.returncode != 0:
        raise RuntimeError(f"could not snapshot reverted tree: {p.stderr.strip()[-200:]}")
    return _git(wt, "rev-parse", "HEAD").stdout.strip()


def build_reverted_commit(repo: Path, culprit: str, head: str, worktree_root: str | None = None) -> RevertBuild:
    with worktree(repo, head, worktree_root) as wt:
        parents = _git(wt, "rev-list", "--parents", "-n", "1", culprit).stdout.split()[1:]
        if not parents:
            return RevertBuild(None, "conflict", "culprit is a root commit: nothing to revert to")
        mainline = ["-m", "1"] if len(parents) > 1 else []
        p = _git(wt, "revert", "--no-commit", *mainline, culprit)
        if p.returncode == 0:
            sha = _snapshot(wt, culprit)
            if sha is None:
                return RevertBuild(None, "empty", "reverting the culprit changes nothing at HEAD (already undone?)")
            return RevertBuild(sha, "clean")
        err = (p.stderr or p.stdout).strip()
        _reset(wt)
        # Conflicts: reverse-apply only the culprit hunks that still fit, leave the rest rejected.
        patch = _git(wt, "diff", parents[0], culprit).stdout
        a = _git(wt, "apply", "-R", "--reject", "--whitespace=nowarn", stdin=patch)
        rejected = a.stderr.count("Rejected hunk")
        for junk in wt.rglob("*.rej"):
            junk.unlink()
        sha = _snapshot(wt, culprit)
        if sha is None:
            return RevertBuild(None, "conflict", f"git revert conflicted and no hunk could be reverted: {err[-200:]}",
                               rejected)
        return RevertBuild(sha, "partial", f"git revert conflicted; reverted the hunks that still apply "
                                           f"({rejected} rejected): {err[-160:]}", rejected)


@dataclass
class RevertOutcome:
    passed: bool | None
    confidence: str
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def decide_revert(rep: CulpritReport, build: RevertBuild, res: CommitResult | None, baseline: float | None,
                  bad_rate: float | None, good_min: float, tolerance: float = TOLERANCE) -> RevertOutcome:
    conf, flags = rep.confidence or "medium", []
    d: dict[str, Any] = {"kind": build.kind, "detail": build.detail, "rejected_hunks": build.rejected,
                         "baseline_pass_rate": baseline, "culprit_pass_rate": bad_rate}
    if build.kind in ("conflict", "partial"):
        flags.append(REVERT_CONFLICTS)
    if res is None or res.verdict == "skip" or res.pass_rate is None:
        d["reverted_verdict"] = res.verdict if res else None
        d["reason"] = (res.reason if res else build.detail) or "revert test could not be evaluated"
        return RevertOutcome(None, downgrade(conf), flags, d)
    d.update(reverted_pass_rate=res.pass_rate, reverted_verdict=res.verdict, reverted_trace_ids=res.trace_ids[:5])
    target = (baseline if baseline is not None else good_min) - tolerance
    fixed = res.pass_rate >= target and (bad_rate is None or res.pass_rate > bad_rate)
    d["target_pass_rate"] = round(target, 4)
    if not fixed:
        flags.append(MULTI_REGRESSION_FLAG)
        return RevertOutcome(False, "low", flags, d)
    if build.kind == "partial":  # proof stands but the revert was not the whole commit: one notch down
        conf = downgrade(conf)
    return RevertOutcome(True, conf, flags, d)


def revert_test(st: BisectState, runner: Runner, sha: str | None = None) -> tuple[RevertOutcome, int]:
    """Run the revert test for `sha` (default: the recorded culprit). Returns (outcome, eval rounds used)."""
    sha = sha or (st.culprit.sha if st.culprit else None)
    if not sha:
        raise ValueError("no culprit to revert-test")
    rep = st.culprit if st.culprit and st.culprit.sha == sha else CulpritReport(sha=sha)
    repo = Path(st.repo)
    head = _git(repo, "rev-parse", "--verify", f"{st.bad_sha or 'HEAD'}^{{commit}}").stdout.strip()
    with step_span("revert_test", culprit=sha, head=head):
        build = build_reverted_commit(repo, sha, head, runner.cfg.worktree_root)
        res, before = None, runner.eval_rounds
        if build.sha:
            # same predicate as the bisect: with a signature, "fixed" means THAT failing check is gone on the
            # reverted HEAD (other regressions' failures do not count against it)
            runner.signature = st.spec.signature if st.spec else runner.signature
            res = runner.evaluate_commit(build.sha, f"{st.id}-revert")
    good = st.results.get(st.good_sha or "")
    bad = st.results.get(sha)
    out = decide_revert(rep, build, res, good.pass_rate if good else None, bad.pass_rate if bad else None,
                        runner.cfg.good_min)
    out.details["reverted_commit"] = build.sha
    if res and res.budget:  # budget signature: same median rule, before = HEAD with the culprit, after = reverted
        head_res = st.results.get(head) or bad
        out.details["budget"] = {"good": budget_view(good.budget if good else None),
                                 "before": budget_view(head_res.budget if head_res else None),
                                 "after": budget_view(res.budget), "repeats": res.repeats}
    if runner.signature:
        out.details["signature"] = runner.signature.key
        out.details["signature_label"] = runner.signature.label
    return out, runner.eval_rounds - before


def confirm_culprit(store: StateStore, st: BisectState, runner: Runner | None = None, *,
                    source: TraceSource | None = None, **analyst_kw: Any) -> CulpritReport:
    """Explain + prove the culprit found by the bisect. Idempotent and resume-safe: each stage is a begin/end step and
    is skipped when already recorded. Status stays `culprit_found` (the Fixer stage moves it on)."""
    from .bisect import make_runner
    if not st.culprit:
        raise ValueError("investigation has no culprit")
    sha = st.culprit.sha
    if not st.culprit.analyzed:
        store.begin_step(st, "culprit_analysis", sha=sha)
        st.culprit = analyze_culprit(st, sha, source=source, **analyst_kw)
        store.end_step(st)
    if "revert_test" not in st.metrics:
        runner = runner or make_runner(store, st)
        store.begin_step(st, "revert_test", sha=sha)
        out, rounds = revert_test(st, runner)
        rep = st.culprit
        rep.revert_test_passed, rep.confidence = out.passed, out.confidence
        rep.flags = list(dict.fromkeys([*rep.flags, *out.flags]))
        rep.revert_details = {**rep.revert_details, **out.details}
        m = st.metrics
        m["revert_test"] = {"passed": out.passed, "eval_rounds": rounds, **{k: out.details.get(k) for k in (
            "kind", "baseline_pass_rate", "reverted_pass_rate", "culprit_pass_rate", "budget")}}
        m["eval_rounds"] = m.get("eval_rounds", 0) + rounds
        m["cost_usd"] = round(m["eval_rounds"] * COST_PER_RUN_USD, 4)
        for f in out.flags:
            if f not in m.setdefault("warnings", []):
                m["warnings"].append(f)
        store.end_step(st)
    return st.culprit


def budget_line(b: dict[str, Any]) -> str:
    """'median tokens: HEAD 31,402 -> reverted 12,150 (good commit 11,873); within budget 1/4 -> 4/4 (3 repeats)'."""
    bef, aft, good = b.get("before") or {}, b.get("after") or {}, b.get("good") or {}
    unit = aft.get("kind") or bef.get("kind") or "metric"
    num = lambda v: "n/a" if v is None else f"{v:,.0f}" if isinstance(v, (int, float)) and v >= 100 else f"{v:g}"  # noqa: E731
    frac = lambda v: f"{v.get('within')}/{v.get('total')}" if v else "n/a"  # noqa: E731
    return (f"median {unit}: HEAD {num(bef.get('median'))} -> reverted {num(aft.get('median'))} "
            f"(good commit {num(good.get('median'))}); within budget {frac(bef)} -> {frac(aft)} "
            f"({b.get('repeats')} repeats per case)")


def describe_revert(st: BisectState) -> list[str]:
    """Lines for `agentbisect status`."""
    c = st.culprit
    if not c:
        return []
    r = {True: "PASSED (regression disappears)", False: "FAILED (regression remains)",
         None: "not run" if "revert_test" not in st.metrics else "inconclusive"}[c.revert_test_passed]
    lines = [f"  culprit: {c.sha} (confidence {c.confidence or '?'})",
             f"  revert test: {r}" + (f"  eval rounds={st.metrics['revert_test']['eval_rounds']}"
                                      if "revert_test" in st.metrics else "")]
    rt = st.metrics.get("revert_test") or {}
    if rt.get("reverted_pass_rate") is not None:
        lines.append(f"    pass rate: culprit {rt.get('culprit_pass_rate')} -> reverted {rt['reverted_pass_rate']} "
                     f"(good commit {rt.get('baseline_pass_rate')})")
    if rt.get("budget"):
        lines.append("    " + budget_line(rt["budget"]))
    if c.intent_source:
        lines.append(f"  intent source: {c.intent_source}")
    for f in c.flags:
        lines.append(f"  flag: {f}")
    return lines
