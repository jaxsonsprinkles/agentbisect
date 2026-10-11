"""Fixer (role 5): a FRESH agent repairs the culprit commit while preserving its intent.

The agent sees only `culprit.fixer_brief` (culprit diff + original intent + failing evidence), the current contents of
the files the culprit touched, and, from attempt 2 on, the previous attempts' edits with their failing-check output.
It never sees the bisect, the other commits, any planted-bug/ground-truth material or the evidence directory; paths
that look like answer keys or secrets are dropped from what it reads (`culprit.PROTECTED`) and refused as edit
targets, as are tests and eval cases (the checks must not be gameable). The one exception is a GUARDED
eval-expectation edit: the structured `eval_edits` field may change `checks` entries of cases of the target signature
(never other fields or cases), and the attempt only passes when the intent judge confirms both that the culprit's
intent is preserved and that the edit merely follows the culprit's deliberate contract change. An inconclusive judge
is not a pass. The same edit may also be applied to HELD-OUT cases, but only for the same check key and tool/argument
contract it changed on a target case in the same attempt (a consistency edit; no target edit means no held-out edit).
Accepted edits are recorded (`eval_expectation_changes`, `held_out_expectation_changes`) and surfaced in the PR body.

Each attempt: fresh worktree at the investigated HEAD -> apply the model's whole-file edits -> commit -> run the
checks (`FixChecker`) on that commit. Up to MAX_ATTEMPTS, stopping at the first pass. The best attempt (the passing
one, else the last that produced a commit) becomes branch `agentbisect/fix-<sha8>`, a patch file and a PR body file.
A DRAFT PR is only opened behind `open_pr=True`, and only when the Fixer's checks passed.

`FixResult.verifier_input` ({"repo", "commit"}) is the stable hand-off to the Verifier.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Collection, Protocol

from .baseline import budget_kind
from .culprit import PROTECTED, fixer_brief, scrub
from .intake import FORBIDDEN_CASE_KEYS
from .models import BisectState, CulpritReport, FixAttempt, FixResult
from .runner import Runner, worktree
from .tracing import step_span
from .verify import (IntentJudge, VerifierInput, _git, _not_clean, _unevaluable, cases_at, check_eval_edits,
                     check_intent, format_all_changes, held_out_violations)

MAX_ATTEMPTS = 3
MAX_FILE_CHARS = 20000
MAX_FAILURE_LINES = 40
DEFAULT_FIXER_MODEL = "claude-sonnet-5-5"
# the checks live here: editing them would let a "fix" pass by changing the question
TEST_PATH = re.compile(r"(^|/)(tests?|evals?)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|(^|/)conftest\.py$|cases\.jsonl$", re.I)
CASES_PATH = "evals/cases.jsonl"
NOGPG = ("-c", "commit.gpgsign=false")

SYSTEM = """You are a software engineer fixing a regression in a repository you have not seen before.
A recent change broke some checks. You get that change's diff, the intent behind it, the failing evidence and the
current contents of the files it touched. Fix the regression with the smallest correct edit.
The commit's intent must be preserved: do not undo or revert the commit, or any part of the behaviour it set out to
introduce, even if that would make the failing checks pass. Fix the CONSEQUENCE of the change instead: whatever else
depends on the changed behaviour and was not updated with it (callers, prompts, tool descriptions, configuration,
documentation). An attempt is also judged on whether the intent survives; one that removes it counts as failed.
Do not edit tests or eval cases to make checks pass.
One narrow exception: if the change DELIBERATELY changed a contract (its stated intent says so) and an eval case still
encodes the old contract, so that no code, prompt or tool change could satisfy both the intent and that case, you may
update that case's expectation, only via "eval_edits" and only for the cases listed under "Eval cases you may update".
If held-out cases are listed, they are a consistency check, not a place to change other behaviour: if (and only if)
you changed that same check and tool/argument contract on a target case, apply the same contract change to the held-out
cases listed, via "eval_edits" too. Never edit a held-out case otherwise.
Say which expectation changes and why, citing the intent. Never loosen a check to hide a defect, and if the code can be
fixed instead, fix the code. An edit is accepted only if a reviewer confirms it merely follows the deliberate change.
Reply with ONLY a JSON object:
{"explanation": "<one or two sentences>", "files": [{"path": "<repo-relative>", "content": "<complete new file>"},
 {"path": "<repo-relative>", "delete": true}],
 "eval_edits": [{"case": "<case id>", "checks": {"<check name>": <complete new value>}, "reason": "<why>"}]}
"files" and "eval_edits" may each be empty or omitted, but not both."""


class FixModel(Protocol):
    def complete(self, system: str, prompt: str) -> str: ...


class FixChecker(Protocol):
    def check(self, commit: str, attempt: int) -> tuple[bool, list[str]]:
        """Run the failing checks on `commit` -> (passed, failing-check output lines)."""


class ProxyFixModel:
    """FixModel on the existing model backend (`LLMProxy.messages`, i.e. the logged-in `claude` CLI)."""

    def __init__(self, proxy: Any = None, model: str | None = None):
        if proxy is None:
            from .llm_proxy import LLMProxy
            proxy = LLMProxy()
        self.proxy, self.model = proxy, model or os.environ.get("FIXER_MODEL") or DEFAULT_FIXER_MODEL

    def complete(self, system: str, prompt: str) -> str:
        resp = self.proxy.messages({"model": self.model, "max_tokens": 16000, "temperature": 0, "system": system,
                                    "messages": [{"role": "user", "content": prompt}]},
                                   salt=f"fixer-{len(prompt)}")
        return "".join(b.get("text", "") for b in resp.get("content", []))


HELD_OUT_REMINDER = ("Held-out cases share the target's check, so a stale held-out expectation means the fix is "
                     "incomplete. If the change DELIBERATELY changed that contract, apply the same contract change to "
                     "those held-out rows via eval_edits (allowed only for the same check and tool/argument contract "
                     "you changed on a target case); otherwise fix the code.")


class RunnerChecker:
    """FixChecker backed by the eval Runner: a commit passes when the signature (or the whole suite) is good and,
    when `st` is given, the held-out cases that share the target's check key also pass (budget:* flags ignored unless
    the target is a budget signature), so an expectation left stale by a contract change fails the attempt."""

    def __init__(self, runner: Runner, run_id: str, st: BisectState | None = None):
        self.runner, self.run_id, self.st = runner, run_id, st

    def _held_out(self, commit: str, attempt: int) -> tuple[bool, list[str]]:
        st = self.st
        held = held_out_sharing_check(st, str(self.runner.repo), commit) if st else []
        if not held:
            return True, []
        rn = self.runner
        saved = rn.signature, rn.ignore_budget
        rn.signature = None  # evaluate the held-out rows themselves, not the target signature
        rn.ignore_budget = not (st.spec and st.spec.signature and budget_kind(st.spec.signature.key))
        try:
            res = rn.evaluate_commit(commit, f"{self.run_id}-fix{attempt}-held", held)
        finally:
            rn.signature, rn.ignore_budget = saved
        if _unevaluable("held_out", "held-out cases", res) is not None:
            return True, []  # infrastructure, not a verdict: the Verifier reports it
        dirty = _not_clean(res, held)
        if res.verdict == "good" and not dirty:
            return True, []
        out = [f"held-out cases failing (same check as the target): {', '.join(dirty or res.failing_cases) or 'band'}"]
        for s in res.failure_signatures:
            out.append(f"held-out failing check {s.get('key')}: expected {s.get('expected')} got {s.get('got')}"
                       + (f" ({s['reason']})" if s.get("reason") else ""))
        out.append(HELD_OUT_REMINDER)
        return False, out[:MAX_FAILURE_LINES]

    def check(self, commit: str, attempt: int) -> tuple[bool, list[str]]:
        res = self.runner.evaluate_commit(commit, f"{self.run_id}-fix{attempt}")
        if res.verdict == "good":
            return self._held_out(commit, attempt)
        out = [f"verdict {res.verdict}" + (f" (pass rate {res.pass_rate:.2f})" if res.pass_rate is not None else "")
               + (f": {res.reason}" if res.reason else "")]
        out += [f"failing case: {c}" for c in res.failing_cases]
        for s in res.failure_signatures:
            out.append(f"failing check {s.get('key')}: expected {s.get('expected')} got {s.get('got')}"
                       + (f" ({s['reason']})" if s.get("reason") else ""))
        return False, out[:MAX_FAILURE_LINES]


# ---------------------------------------------------------------- git helpers

def _must(repo: Path | str, *args: str) -> str:
    p = _git(Path(repo), *args)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def _readable(rel: str) -> bool:
    p = Path(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and not PROTECTED.search(rel)


def _editable(rel: str) -> bool:
    return _readable(rel) and not TEST_PATH.search(rel)


# ---------------------------------------------------------------- prompt + reply

def target_eval_cases(st: BisectState, repo: str, head: str) -> list[str]:
    """Ids of the cases whose expectations the Fixer may update: the target signature's cases (none when the
    investigation has no signature), minus held-out ones, that exist at `head`."""
    sig = st.spec.signature if st.spec else None
    rows = {r["id"]: r for r in cases_at(repo, head)}
    return [c for c in (sig.cases if sig else []) if c in rows and not rows[c].get("held_out")]


def held_out_eval_cases(st: BisectState, repo: str, head: str) -> list[str]:
    """Ids of the held-out cases (recorded set plus rows flagged `held_out`) that exist at `head`. Their expectations
    may only follow a target-case contract edit made in the same attempt."""
    rows = cases_at(repo, head)
    held = {*(st.spec.held_out if st.spec else []), *(r["id"] for r in rows if r.get("held_out"))}
    return [r["id"] for r in rows if r["id"] in held]


def held_out_sharing_check(st: BisectState, repo: str, commit: str) -> list[str]:
    """Held-out cases at `commit` whose `checks` carry the target signature's check key (e.g. call_args)."""
    sig = st.spec.signature if st.spec else None
    if not sig or not sig.check:
        return []
    held = set(held_out_eval_cases(st, repo, commit))
    return [r["id"] for r in cases_at(repo, commit)
            if r["id"] in held and sig.check in (r.get("checks") or {})]


def build_prompt(repo: str, report: CulpritReport, head: str, previous: list[FixAttempt],
                 eval_cases: Collection[str] = (), held_cases: Collection[str] = ()) -> tuple[str, set[str]]:
    """-> (prompt, oversize). Brief + current file contents + earlier attempts; nothing the Fixer may not know.
    `oversize` are touched files too big to show whole: edits to them are refused (a whole-file replace of a file
    the model only half saw would drop the rest)."""
    parts = [fixer_brief(repo, report)]
    paths = _must(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-z", report.sha).split("\0")
    cur, oversize = [], set()
    for f in filter(_readable, paths):
        p = _git(Path(repo), "show", f"{head}:{f}")
        if p.returncode != 0:  # file may have been deleted since the culprit
            continue
        if len(p.stdout) > MAX_FILE_CHARS:
            oversize.add(f)
            cur.append(f"### {f}\n(too large to show; you may not edit this file)")
        else:
            cur.append(f"### {f}\n```\n{p.stdout}\n```")
    if cur:
        parts.append("## Current contents of the files the change touched\n\n" + "\n\n".join(cur))
    rows = [r for r in cases_at(repo, head) if r["id"] in eval_cases]
    if rows:
        shown = "\n".join(scrub(json.dumps({k: v for k, v in r.items() if k not in FORBIDDEN_CASE_KEYS})) for r in rows)
        parts.append("## Eval cases you may update (only via eval_edits, only if the change deliberately changed "
                     f"their contract)\n\n```\n{shown}\n```")
    if rows and held_cases:  # only the checks the target rows carry: the contract a consistency edit could follow
        keys = {k for r in rows for k, v in (r.get("checks") or {}).items() if isinstance(v, dict)}
        held = [{"id": r["id"], "checks": {k: v for k, v in (r.get("checks") or {}).items() if k in keys}}
                for r in cases_at(repo, head) if r["id"] in held_cases]
        held = [h for h in held if h["checks"]]
        if held:
            parts.append("## Held-out eval cases (consistency check only; not a place to change other behaviour)\n\n"
                         "These held-out rows exist to catch overfitting. You may update one ONLY via eval_edits, ONLY "
                         "the check key listed, and ONLY if you changed that same check and tool/argument contract on "
                         "an allowed case above in this same reply.\n\n```\n"
                         + "\n".join(scrub(json.dumps(h)) for h in held) + "\n```")
    for a in previous:
        why = scrub(a.error or "\n".join(f"- {x}" for x in a.failures)) or "(no output)"
        parts.append(f"## Previous attempt {a.attempt} did NOT fix it\n\nEdited: {', '.join(a.files) or '(nothing)'}\n"
                     f"Its change:\n```diff\n{scrub(a.diff[:MAX_FILE_CHARS])}\n```\nFailing-check output:\n{why}\n\n"
                     "Fix the cause; do not repeat that attempt."
                     + (" That attempt's checks passed but it undid the intent of the change: keep the intent and "
                        "fix its consequence elsewhere." if a.intent_test_passed is False else ""))
    return "\n\n".join(parts), oversize


def parse_reply(text: str) -> tuple[str, list[dict[str, Any]]]:
    """-> (explanation, edits). Raises ValueError with a message the next attempt can use."""
    d = None
    try:
        d = json.loads(text)
    except ValueError:
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
        body = m.group(1) if m else text[text.find("{"):text.rfind("}") + 1]
        try:
            d = json.loads(body)
        except ValueError as e:
            raise ValueError(f"reply was not valid JSON ({e})") from e
    files = d.get("files", []) if isinstance(d, dict) else None
    edits = d.get("eval_edits") if isinstance(d, dict) else None
    if not isinstance(files, list) or (not files and not edits):
        raise ValueError('reply has no non-empty "files" list')
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not (
                f.get("delete") is True or isinstance(f.get("content"), str)):
            raise ValueError('every file needs a string "path" and either "content" or "delete": true')
    return str(d.get("explanation") or ""), files


def parse_eval_edits(text: str) -> list[dict[str, Any]]:
    """The reply's `eval_edits` ([] when absent). Each needs a case id, a non-empty `checks` object and a reason."""
    try:
        d = json.loads(text)
    except ValueError:
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
        d = json.loads(m.group(1) if m else text[text.find("{"):text.rfind("}") + 1])
    edits = d.get("eval_edits") or []
    if not isinstance(edits, list):
        raise ValueError('"eval_edits" must be a list')
    for e in edits:
        if (not isinstance(e, dict) or not isinstance(e.get("case"), str) or not isinstance(e.get("checks"), dict)
                or not e["checks"] or not isinstance(e.get("reason"), str) or not e["reason"].strip()):
            raise ValueError('every eval edit needs a string "case", a non-empty "checks" object and a "reason"')
    return edits


def apply_eval_edits(wt: Path, edits: list[dict[str, Any]], allowed: Collection[str],
                     held_allowed: Collection[str] = ()) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Update `checks` entries of the allowed cases in the worktree's cases file (other lines are left byte-identical).
    Raises ValueError, writing nothing, for a case outside `allowed`/`held_allowed`, a missing row, or a no-op.
    -> ([target changes], [held-out changes]), each {"case", "key", "old", "new", "reason"}; the caller checks that
    the held-out changes follow a target change (`verify.held_out_violations`)."""
    target = wt / CASES_PATH
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"eval edit refused: {CASES_PATH} is missing or a symlink")
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    rows: dict[str, tuple[int, dict]] = {}
    for i, ln in enumerate(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            rows[r["id"]] = (i, r)
    changes: list[dict[str, Any]] = []
    held_changes: list[dict[str, Any]] = []
    for e in edits:
        cid = e["case"]
        is_held = cid in held_allowed and cid not in allowed
        if cid not in allowed and not is_held:
            raise ValueError(f"eval edit refused: case {cid!r} is not a failing case of the target signature "
                             "or a listed held-out case")
        if cid not in rows or (rows[cid][1].get("held_out") and not is_held):
            raise ValueError(f"eval edit refused: case {cid!r} does not exist or is held out")
        i, row = rows[cid]
        checks = row.get("checks")
        if not isinstance(checks, dict):
            raise ValueError(f"eval edit refused: case {cid!r} has no checks object")
        for k, new in e["checks"].items():
            if checks.get(k) != new:
                (held_changes if is_held else changes).append(
                    {"case": cid, "key": k, "old": checks.get(k), "new": new, "reason": e["reason"].strip()})
                checks[k] = new
    if not changes and not held_changes:
        raise ValueError("the eval edits changed nothing")
    touched = {c["case"] for c in [*changes, *held_changes]}
    for cid, (i, row) in rows.items():
        if cid in touched:
            lines[i] = json.dumps(row, ensure_ascii=False) + ("\n" if lines[i].endswith("\n") else "")
    target.write_text("".join(lines), encoding="utf-8")
    return changes, held_changes


def apply_edits(wt: Path, edits: list[dict[str, Any]], blocked: Collection[str] = ()) -> list[str]:
    """Validate every edit, then write them. Raises ValueError (nothing is written if validation fails)."""
    root = wt.resolve()
    for e in edits:
        rel = e["path"]
        if not _editable(rel):
            raise ValueError(f"refused to edit {rel!r}: outside the repo, tests/eval cases, or a protected path")
        if rel in blocked:
            raise ValueError(f"refused to edit {rel!r}: file was too large to show in full")
        t = wt / rel
        if t.is_symlink() or not t.resolve().is_relative_to(root):
            raise ValueError(f"refused to edit {rel!r}: symlink or path escapes the repository")
    try:
        for e in edits:
            t = wt / e["path"]
            if e.get("delete"):
                t.unlink(missing_ok=True)
            else:
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(e["content"], encoding="utf-8")
    except OSError as err:
        raise ValueError(f"could not apply edits: {err}") from err
    return [e["path"] for e in edits]


# ---------------------------------------------------------------- the Fixer

def branch_name(sha: str) -> str:
    return f"agentbisect/fix-{sha[:8]}"


def run_fixer(st: BisectState, *, model: FixModel, checker: FixChecker, out_dir: str | Path | None = None,
              max_attempts: int = MAX_ATTEMPTS, open_pr: bool = False, base: str | None = None,
              run: Callable[..., Any] = subprocess.run, worktree_root: str | None = None,
              judge: IntentJudge | None = None) -> FixResult:
    """Run up to `max_attempts` fix attempts for st.culprit. The attempts are also recorded on st.fix_attempts.

    An attempt only passes when the checks pass AND `judge` (verify.check_intent on the attempt's diff) says the
    culprit's intent survives, so reverting the culprit counts as failed and the judge's reason goes into the next
    prompt. A missing/failed judge is inconclusive: it does not block, and is recorded in metrics["fix"]."""
    rep = st.culprit
    if not rep:
        raise ValueError("investigation has no culprit to fix")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    repo = Path(st.repo).resolve()
    head = _must(repo, "rev-parse", "--verify", f"{base or st.bad_sha or 'HEAD'}^{{commit}}").strip()
    branch = branch_name(rep.sha)
    res = FixResult(base=head, branch=branch)
    inconclusive: list[dict[str, Any]] = []  # attempts whose intent check could not be judged
    best: FixAttempt | None = None  # passing attempt, else the last one that produced a commit
    for n in range(1, max_attempts + 1):
        att = FixAttempt(branch=branch, attempt=n)
        res.attempts.append(att)
        with step_span("fix_attempt", attempt=n, culprit=rep.sha):
            try:
                allowed = target_eval_cases(st, str(repo), head)
                held_cases = held_out_eval_cases(st, str(repo), head)
                prompt, oversize = build_prompt(str(repo), rep, head, res.attempts[:-1], allowed, held_cases)
                reply = model.complete(SYSTEM, prompt)
                att.explanation, edits = parse_reply(reply)
                eval_edits = parse_eval_edits(reply)
                with worktree(repo, head, worktree_root) as wt:
                    att.files = apply_edits(wt, edits, oversize)
                    if eval_edits:
                        att.eval_expectation_changes, att.held_out_expectation_changes = apply_eval_edits(
                            wt, eval_edits, allowed, held_cases)
                        if bad := held_out_violations(att.eval_expectation_changes, att.held_out_expectation_changes):
                            att.eval_expectation_changes, att.held_out_expectation_changes = [], []
                            raise ValueError("held-out eval edit refused: " + "; ".join(bad))
                    _must(wt, "add", "-A")
                    if _git(wt, "diff", "--cached", "--quiet").returncode == 0:
                        raise ValueError("the edits changed nothing")
                    _must(wt, *NOGPG, "commit", "-q", "--no-verify", "-m",
                          f"agentbisect fix attempt {n} for {rep.sha[:10]}")
                    att.commit = _must(wt, "rev-parse", "HEAD").strip()
                    att.diff = _must(wt, "diff", head, att.commit)
            except ValueError as e:  # bad reply / forbidden path / no-op: tell the next attempt
                att.error, att.passed = str(e), False
                continue
            att.passed, att.failures = checker.check(att.commit, n)
            if att.passed:
                iv = check_intent(st, VerifierInput(str(repo), att.commit, branch, head), head, judge)
                att.intent_test_passed = iv.passed
                if iv.passed is False:
                    att.passed, att.failures = False, [f"intent not preserved: {iv.detail}"]
                elif iv.passed is None:
                    inconclusive.append({"attempt": n, "detail": iv.detail})
                if att.passed is not False and (att.eval_expectation_changes or att.held_out_expectation_changes):
                    # an expectation edit is only accepted on a positive intent verdict AND a positive eval-edit verdict
                    ev = check_eval_edits(st, VerifierInput(str(repo), att.commit, branch, head), judge,
                                          att.eval_expectation_changes,
                                          att.held_out_expectation_changes) if iv.passed else iv
                    if iv.passed is not True or ev.passed is not True:
                        att.passed = False
                        att.failures = ["eval expectation edit not accepted (" + "; ".join(format_all_changes(
                            att.eval_expectation_changes, att.held_out_expectation_changes)) + "): " + ev.detail
                            + ("" if ev.passed is not None else " (judge inconclusive)")]
        best = att
        if att.passed:
            break
    st.fix_attempts = list(res.attempts)
    if best:
        _must(repo, "branch", "-f", branch, best.commit)
        res.patch = best.diff
        res.verifier_input = {"repo": str(repo), "commit": best.commit}
        res.passed = bool(best.passed)
        res.eval_expectation_changes = list(best.eval_expectation_changes)
        res.held_out_expectation_changes = list(best.held_out_expectation_changes)
    if open_pr and res.passed:
        try:
            res.pr_url = _open_pr(st, run)
        except (RuntimeError, ValueError) as e:
            res.pr_error = str(e)
    if out_dir:
        _write_files(st, res, Path(out_dir))
    st.metrics["fix"] = {"branch": res.branch, "passed": res.passed, "base": res.base,
                         "verifier_input": res.verifier_input, "pr_url": res.pr_url, "pr_error": res.pr_error,
                         "intent_inconclusive": inconclusive,
                         "eval_expectation_changes": res.eval_expectation_changes,
                         "held_out_expectation_changes": res.held_out_expectation_changes}
    return res


def _write_files(st: BisectState, res: FixResult, out: Path) -> None:
    from .report import build_report, render_markdown
    out.mkdir(parents=True, exist_ok=True)
    if res.patch:
        res.patch_path = str(out / f"fix-{st.id}.patch")
        Path(res.patch_path).write_text(res.patch)
    res.pr_body_path = str(out / f"fix-{st.id}-pr.md")
    Path(res.pr_body_path).write_text(render_markdown(build_report(st, st.repo), pr=True))


def _open_pr(st: BisectState, run: Callable[..., Any]) -> str:
    """Push the fix branch and open a DRAFT PR through the reporter (always draft: the Verifier decides it is good)."""
    from .report import open_pr
    out = open_pr(st, st.repo, draft=True, confirm=True, run=run, say=lambda _: None)
    return out["url"]
