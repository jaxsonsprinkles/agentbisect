"""Verifier: the revert test that proves the culprit (chunk 9) and fix verification (chunk 11, `verify_fix`, below).

Revert test: in a fresh worktree at the investigated HEAD, `git revert --no-commit <culprit>`, snapshot the result as a
throwaway commit (so the Runner, cache and worktree machinery work unchanged), run the suite cases on it and require
the regression to disappear: pass rate back to the good-commit level within `tolerance`.

  * revert conflicts   -> abort, retry with only the culprit's hunks that still apply (`git apply -R --reject`),
                          flag "revert conflicts" and cap confidence. Nothing applicable -> inconclusive (None).
  * revert does not fix -> the culprit is probably wrong or there are several regressions: confidence low and the
                          "multiple regressions likely" flag.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .bisect import COST_PER_RUN_USD
from .culprit import analyze_culprit
from .intake import TraceSource
from .models import BisectState, CommitResult, CulpritReport, FixAttempt
from .llm_proxy import DUMMY_API_KEY
from .runner import BuildError, EvalInfraError, Runner, worktree
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


# ======================================================================================================== fix verification
#
# verify_fix(): four checks on a candidate fix commit, each with its own verdict (True / False / None = inconclusive).
#   target     the culprit's failure signature no longer reproduces (median rule for budget signatures, baseline.py)
#   regression no case that passed before the fix (at the pre-fix commit) fails with it
#   held_out   the held-out cases (cases.jsonl `held_out`) pass, so the fix is not overfit to the working suite
#   intent     a judge (pinned Sonnet) says the fix still preserves the culprit's stated intent (Entire checkpoint)
# The fix passes only when all four are True. Cases are evaluated through the same Runner, cache and pins as the bisect.

JUDGE_TIER_RE = re.compile(r"sonnet|opus|fable", re.I)  # roles: the judge must be above haiku (ROLES.md)
MAX_JUDGE_DIFF_CHARS = 20000
FLAKY_RERUN_REPEATS = 5  # repeats for cases with mixed results in the regression check
CHECK_ORDER = ("target", "regression", "held_out", "intent")
ANTHROPIC_API = "https://api.anthropic.com"


@dataclass
class VerifierInput:
    """What the Fixer hands the Verifier: a repo and the candidate fix commit (on a fix branch). `base` is the commit the
    fix was written against (default: the investigation's bad end); the regression check compares against it."""
    repo: str
    commit: str
    branch: str = ""
    base: str | None = None


@dataclass
class CheckVerdict:
    name: str
    passed: bool | None  # None = inconclusive (could not be evaluated)
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail, "data": self.data}


@dataclass
class VerifyResult:
    commit: str
    branch: str = ""
    checks: list[CheckVerdict] = field(default_factory=list)
    eval_rounds: int = 0

    @property
    def passed(self) -> bool | None:
        """True only when every check passed; False if any failed; None if none failed but some are inconclusive."""
        vs = [c.passed for c in self.checks]
        if any(v is False for v in vs):
            return False
        return True if vs and all(v is True for v in vs) else None

    def check(self, name: str) -> CheckVerdict | None:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {"commit": self.commit, "branch": self.branch, "passed": self.passed, "eval_rounds": self.eval_rounds,
                "checks": [c.to_dict() for c in self.checks]}


@dataclass
class IntentVerdict:
    preserved: bool
    reason: str = ""
    model: str = ""


class IntentJudge(Protocol):
    """Judges whether a fix preserves a commit's intent; tests pass fakes."""

    def judge(self, intent: str, culprit_diff: str, fix_diff: str) -> IntentVerdict: ...


INTENT_SYSTEM = ("You are a strict code reviewer. A commit (the CULPRIT) was written with a stated intent but also "
                 "introduced a regression. A FIX was then written. Decide ONLY whether the fix still preserves the "
                 "culprit's stated intent: the feature or behaviour the culprit set out to add must still work. A fix "
                 "that simply reverts the culprit (undoing the intended feature) does NOT preserve the intent. "
                 'Reply with JSON only: {"preserved": true|false, "reason": "<one or two sentences>"}.')


def parse_intent_reply(text: str) -> tuple[bool, str]:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict) or not isinstance(d.get("preserved"), bool):
        raise ValueError(f"judge reply has no boolean 'preserved': {text[:200]!r}")
    return d["preserved"], str(d.get("reason", ""))[:500]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_JUDGE_DIFF_CHARS else text[:MAX_JUDGE_DIFF_CHARS] + "\n... (truncated)"


class ClaudeIntentJudge:
    """The Sonnet-pinned judge: one Messages call, to the llm-proxy (own `claude` login) when
    AGENTBISECT_LLM_PROXY_URL is set, else the Anthropic API with ANTHROPIC_API_KEY. Refuses a haiku-tier or unset
    model (the roles rule that the judge outranks the agent), and a response that does not echo the pinned model."""

    def __init__(self, model: str, url: str | None = None, timeout: float = 300.0):
        if not model or not JUDGE_TIER_RE.search(model):
            raise ValueError(f"intent judge model {model!r} is not above haiku tier: refusing to judge")
        self.model, self.timeout = model, timeout
        self.url = (url or os.environ.get("AGENTBISECT_LLM_PROXY_URL") or ANTHROPIC_API).rstrip("/")

    def judge(self, intent: str, culprit_diff: str, fix_diff: str) -> IntentVerdict:
        user = (f"## Culprit's stated intent\n\n{intent or '(unknown)'}\n\n"
                f"## Culprit diff\n\n```diff\n{_cap(culprit_diff)}\n```\n\n"
                f"## Fix diff (applied on top of the culprit's tree)\n\n```diff\n{_cap(fix_diff)}\n```\n")
        key = os.environ.get("ANTHROPIC_API_KEY", "") if self.url == ANTHROPIC_API else DUMMY_API_KEY
        req = urllib.request.Request(
            self.url + "/v1/messages",
            json.dumps({"model": self.model, "max_tokens": 400, "temperature": 0, "system": INTENT_SYSTEM,
                        "messages": [{"role": "user", "content": user}]}).encode(),
            {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read())
        recorded = resp.get("model") or ""
        if recorded != self.model:
            raise ValueError(f"judge answered with model {recorded!r}, not the pinned {self.model!r}")
        preserved, reason = parse_intent_reply("".join(b.get("text", "") for b in resp.get("content", [])))
        return IntentVerdict(preserved, reason, recorded)


def _git_out(repo: str, *args: str) -> str:
    p = _git(Path(repo), *args)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[-200:]}")
    return p.stdout


def _resolve(repo: str, rev: str) -> str:
    return _git_out(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").strip()


def _pr(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _stats(res: CommitResult, cases: list[str]) -> dict[str, tuple[int, int]]:
    """case -> (passes, fails) at a commit; cases with no executed run are absent."""
    out = {}
    for c in cases:
        t = res.case_stats.get(c)
        if t and t["passes"] + t["fails"]:
            out[c] = (t["passes"], t["fails"])
    return out


def _not_clean(res: CommitResult, cases: list[str]) -> list[str]:
    """Cases with any failing or flaky run, or none executed: one bad case among many must not hide in the aggregate."""
    st = _stats(res, cases)
    return [c for c in cases if c not in st or st[c][1] > 0]


def _unevaluable(name: str, what: str, res: CommitResult) -> CheckVerdict | None:
    if res.verdict == "skip" or res.pass_rate is None:
        return CheckVerdict(name, None, f"{what} could not be evaluated: {res.reason or 'no executed runs'}",
                            {"verdict": res.verdict})
    return None


def check_target(runner: Runner, st: BisectState, fix: str, run_id: str) -> CheckVerdict:
    sig = st.spec.signature if st.spec else None
    runner.signature = sig
    res = runner.evaluate_commit(fix, run_id)  # cache hits for runs the regression check already made
    if (bad := _unevaluable("target", "fix", res)):
        return bad
    cases = list(sig.cases if sig and sig.cases else st.spec.cases)
    dirty = _not_clean(res, cases)
    data: dict[str, Any] = {"signature": sig.key if sig else None, "verdict": res.verdict, "pass_rate": res.pass_rate,
                            "failing_cases": res.failing_cases, "flaky_cases": res.flaky_cases, "not_clean": dirty,
                            "executed": res.executed}
    if res.budget:
        data["budget"] = budget_view(res.budget)
    label = sig.label if sig else "the suite's failing cases"
    if res.verdict == "good" and not dirty:
        return CheckVerdict("target", True, f"{label} no longer reproduces in any of {len(cases)} cases "
                                            f"(pass rate {_pr(res.pass_rate)})", data)
    return CheckVerdict("target", False, f"{label} still reproduces ({res.verdict}, pass rate {_pr(res.pass_rate)}; "
                                         f"cases still failing or flaky: {', '.join(dirty) or 'none individually'})", data)


def _regressed(before: tuple[int, int], after: tuple[int, int] | None) -> bool:
    """Count rule. Deterministic before (no failures): any failure after is a regression, unless the case is flaky after
    (mixed results) in which case it needs 2 more failures than the baseline failure rate predicts. Flaky before: the
    fix must not fail more than 1 run above the rate that the pre-fix commit's failure rate predicts."""
    if after is None:
        return True  # no result at the fix: cannot show it still passes
    (bp, bf), (ap, af) = before, after
    expected = bf / (bp + bf) * (ap + af)  # failures the pre-fix failure rate predicts for this many runs
    flaky = bf > 0 or (af > 0 and ap > 0)
    return af - expected > (1 if flaky else 0)


def check_regression(runner: Runner, st: BisectState, fix: str, base: str, run_id: str) -> CheckVerdict:
    """Cases that pass at `base` (the pre-fix commit) must still pass with the fix: per case, signature off so any
    failure counts. Cases with mixed results at either commit are re-run with more repeats first."""
    runner.signature = None
    cases = list(st.spec.cases)
    before, after = runner.evaluate_commit(base, f"{run_id}-base", cases), runner.evaluate_commit(fix, run_id, cases)
    for r, what in ((before, "pre-fix commit"), (after, "fix")):
        if (bad := _unevaluable("regression", what, r)):
            return bad
    b, a = _stats(before, cases), _stats(after, cases)
    # a single failure may be noise and a missing result may be an error: re-sample those cases, then compare counts
    mixed = [c for c in cases if c not in a or b.get(c, (1, 0))[1] > 0 or a[c][1] > 0]
    if mixed:
        saved = runner.cfg.repeats
        runner.cfg.repeats = max(saved, FLAKY_RERUN_REPEATS)
        try:
            before = runner.evaluate_commit(base, f"{run_id}-base", mixed)
            after = runner.evaluate_commit(fix, run_id, mixed)
        finally:
            runner.cfg.repeats = saved
        b, a = {**b, **_stats(before, mixed)}, {**a, **_stats(after, mixed)}
        if (bad := _unevaluable("regression", "re-run", after)) or (bad := _unevaluable("regression", "re-run", before)):
            return bad
    passing = [c for c in cases if c in b and b[c][0] / sum(b[c]) >= 0.5]
    regressed = [c for c in passing if _regressed(b[c], a.get(c))]
    data = {"cases_passing_before": passing, "regressed": regressed, "rerun_cases": mixed,
            "pass_rate_before": before.pass_rate, "pass_rate_after": after.pass_rate,
            "score": (len(passing) - len(regressed)) / len(passing) if passing else None}
    if regressed:
        return CheckVerdict("regression", False, f"{len(regressed)}/{len(passing)} previously passing cases regress: "
                            + ", ".join(regressed), data)
    return CheckVerdict("regression", True, f"all {len(passing)} previously passing cases still pass"
                        + (f" ({len(mixed)} flaky re-run)" if mixed else ""), data)


def check_held_out(runner: Runner, fix: str, held: list[str], rubric_cases: list[str], run_id: str) -> CheckVerdict:
    if not held:
        return CheckVerdict("held_out", None, "no held-out cases defined: cannot check for overfitting")
    runner.signature = None
    saved = runner.cfg.judge_cases
    runner.cfg.judge_cases = tuple(dict.fromkeys([*saved, *(c for c in rubric_cases if c in held)]))  # judge repeats
    try:
        res = runner.evaluate_commit(fix, f"{run_id}-held", held)
    finally:
        runner.cfg.judge_cases = saved
    if (bad := _unevaluable("held_out", "held-out cases", res)):
        return bad
    dirty = _not_clean(res, held)
    data = {"cases": held, "pass_rate": res.pass_rate, "failing_cases": res.failing_cases,
            "flaky_cases": res.flaky_cases, "not_clean": dirty, "verdict": res.verdict}
    ok = res.verdict == "good" and not dirty
    return CheckVerdict("held_out", ok, f"held-out pass rate {_pr(res.pass_rate)} over {', '.join(held)}"
                        + ("" if ok else f" (failing or flaky: {', '.join(dirty) or 'band'})"), data)


def check_intent(st: BisectState, vin: VerifierInput, base: str, judge: IntentJudge | None) -> CheckVerdict:
    c = st.culprit
    if not c or not c.checkpoint_intent:
        return CheckVerdict("intent", None, "no culprit intent recorded (run the culprit analysis first)")
    if judge is None:
        return CheckVerdict("intent", None, "no judge available")
    try:
        culprit_diff = _git_out(vin.repo, "show", "-m", "--first-parent", "--format=", "--patch", c.sha, "--")
        fix_diff = _git_out(vin.repo, "diff", base, vin.commit, "--")
        v = judge.judge(c.checkpoint_intent, culprit_diff, fix_diff)
    except Exception as e:  # a judge outage is inconclusive, never a pass or a fail
        return CheckVerdict("intent", None, f"intent judge failed: {type(e).__name__}: {e}")
    src = "checkpoint" if c.intent_source == "checkpoint" else "commit message, no checkpoint"
    return CheckVerdict("intent", v.preserved, f"judge ({v.model or 'unknown model'}; intent from {src}): {v.reason}",
                        {"judge_model": v.model, "intent_source": c.intent_source})


def cases_at(repo: str, commit: str) -> list[dict]:
    """Rows of evals/cases.jsonl as committed at `commit` ([] when absent or unreadable)."""
    p = _git(Path(repo), "show", f"{commit}:evals/cases.jsonl")
    rows = []
    for line in (p.stdout.splitlines() if p.returncode == 0 else []):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def held_out_cases(st: BisectState, repo: str, fix: str) -> tuple[list[str], list[str]]:
    """-> (held-out ids, ids with a judge rubric). Union of the investigation's recorded held-out set and the cases
    flagged `held_out` in the fix commit's own cases file (a fix cannot shrink the set by editing the file)."""
    rows = cases_at(repo, fix)
    held = list(dict.fromkeys([*(st.spec.held_out if st.spec else []), *(r["id"] for r in rows if r.get("held_out"))]))
    return held, [r["id"] for r in rows if r.get("rubric") and r["id"] in held]


def _inconclusive(name: str, e: Exception) -> CheckVerdict:
    return CheckVerdict(name, None, f"could not be evaluated: {type(e).__name__}: {e}")


def verify_fix(st: BisectState, runner: Runner, vin: VerifierInput, judge: IntentJudge | None = None) -> VerifyResult:
    """Run the four checks on `vin.commit`. Everything is resolved in the runner's repo (a different `vin.repo` is an
    error). Infrastructure failures make a check inconclusive. Never mutates `st` (see record_verification)."""
    if not st.spec or not st.spec.cases:
        raise ValueError("investigation has no eval cases")
    repo = str(runner.repo)
    if Path(vin.repo).resolve() != runner.repo:
        raise ValueError(f"fix repo {vin.repo} is not the repo being evaluated ({repo})")
    fix = _resolve(repo, vin.commit)
    base = _resolve(repo, vin.base or st.bad_sha or "HEAD")
    run_id, before, saved = f"{st.id}-verify-{fix[:10]}", runner.eval_rounds, runner.signature
    held, rubric = held_out_cases(st, repo, fix)

    def guarded(name: str, fn) -> CheckVerdict:
        try:
            return fn()
        except (EvalInfraError, BuildError) as e:  # no verdict is not a verdict; pin/baseline errors still raise
            return _inconclusive(name, e)

    out = VerifyResult(fix, vin.branch)
    try:
        with step_span("verify_fix", fix=fix, base=base):
            out.checks = [
                guarded("regression", lambda: check_regression(runner, st, fix, base, run_id)),
                guarded("target", lambda: check_target(runner, st, fix, run_id)),
                guarded("held_out", lambda: check_held_out(runner, fix, held, rubric, run_id)),
                check_intent(st, VerifierInput(repo, fix, vin.branch, base), base, judge)]
        out.checks.sort(key=lambda c: CHECK_ORDER.index(c.name))
    finally:
        runner.signature = saved
    out.eval_rounds = runner.eval_rounds - before
    return out


def _same_commit(repo: str, ref: str, commit: str) -> bool:
    try:
        return _resolve(repo, ref) == commit
    except RuntimeError:
        return False


def record_verification(store: StateStore, st: BisectState, vin: VerifierInput, res: VerifyResult) -> FixAttempt:
    """Store the result on the matching FixAttempt (by branch, else a new one) and in metrics; moves the investigation
    to `verifying` when the state machine allows it."""
    reg, held, intent = res.check("regression"), res.check("held_out"), res.check("intent")
    att = next((a for a in st.fix_attempts if a.branch == res.commit or (vin.branch and a.branch == vin.branch)
                or _same_commit(st.repo, a.branch, res.commit)), None)
    if att is None:
        att = FixAttempt(branch=vin.branch or res.commit, attempt=len(st.fix_attempts) + 1)
        st.fix_attempts.append(att)
    att.eval_score = reg.data.get("score") if reg else None
    att.held_out_score = held.data.get("pass_rate") if held else None
    att.intent_test_passed = intent.passed if intent else None
    st.metrics.setdefault("verify_fix", {})[res.commit] = res.to_dict()
    st.metrics["eval_rounds"] = st.metrics.get("eval_rounds", 0) + res.eval_rounds
    st.metrics["cost_usd"] = round(st.metrics["eval_rounds"] * COST_PER_RUN_USD, 4)
    if st.status in ("culprit_found", "fixing"):
        store.set_status(st, "verifying")
    else:
        store.save(st)
    return att


def describe_verification(res: VerifyResult) -> list[str]:
    mark = {True: "PASS", False: "FAIL", None: "INCONCLUSIVE"}
    head = {True: "VERIFIED", False: "NOT VERIFIED", None: "INCONCLUSIVE"}[res.passed]
    lines = [f"fix {res.commit[:10]}" + (f" ({res.branch})" if res.branch else "") + f": {head}"
             f"  eval rounds={res.eval_rounds}"]
    lines += [f"  [{mark[c.passed]}] {c.name}: {c.detail}" for c in res.checks]
    return lines
