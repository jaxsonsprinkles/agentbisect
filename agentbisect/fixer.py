"""Fixer (role 5): a FRESH agent repairs the culprit commit while preserving its intent.

The agent sees only `culprit.fixer_brief` (culprit diff + original intent + failing evidence), the current contents of
the files the culprit touched, and, from attempt 2 on, the previous attempts' edits with their failing-check output.
It never sees the bisect, the other commits, any planted-bug/ground-truth material or the evidence directory; paths
that look like answer keys or secrets are dropped from what it reads (`culprit.PROTECTED`) and refused as edit
targets, as are tests and eval cases (the checks must not be gameable).

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

from .culprit import PROTECTED, fixer_brief, scrub
from .models import BisectState, CulpritReport, FixAttempt, FixResult
from .runner import Runner, worktree
from .tracing import step_span
from .verify import _git

MAX_ATTEMPTS = 3
MAX_FILE_CHARS = 20000
MAX_FAILURE_LINES = 40
DEFAULT_FIXER_MODEL = "claude-sonnet-5-5"
# the checks live here: editing them would let a "fix" pass by changing the question
TEST_PATH = re.compile(r"(^|/)(tests?|evals?)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|(^|/)conftest\.py$|cases\.jsonl$", re.I)
NOGPG = ("-c", "commit.gpgsign=false")

SYSTEM = """You are a software engineer fixing a regression in a repository you have not seen before.
A recent change broke some checks. You get that change's diff, the intent behind it, the failing evidence and the
current contents of the files it touched. Fix the regression with the smallest correct edit while KEEPING what the
change set out to do (its intent); do not simply revert it unless the intent is void. Do not edit tests or eval
cases to make checks pass. Reply with ONLY a JSON object:
{"explanation": "<one or two sentences>", "files": [{"path": "<repo-relative>", "content": "<complete new file>"},
 {"path": "<repo-relative>", "delete": true}]}"""


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


class RunnerChecker:
    """FixChecker backed by the eval Runner: a commit passes when the signature (or the whole suite) is good."""

    def __init__(self, runner: Runner, run_id: str):
        self.runner, self.run_id = runner, run_id

    def check(self, commit: str, attempt: int) -> tuple[bool, list[str]]:
        res = self.runner.evaluate_commit(commit, f"{self.run_id}-fix{attempt}")
        if res.verdict == "good":
            return True, []
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

def build_prompt(repo: str, report: CulpritReport, head: str, previous: list[FixAttempt]) -> tuple[str, set[str]]:
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
    for a in previous:
        why = scrub(a.error or "\n".join(f"- {x}" for x in a.failures)) or "(no output)"
        parts.append(f"## Previous attempt {a.attempt} did NOT fix it\n\nEdited: {', '.join(a.files) or '(nothing)'}\n"
                     f"Its change:\n```diff\n{scrub(a.diff[:MAX_FILE_CHARS])}\n```\nFailing-check output:\n{why}\n\n"
                     "Fix the cause; do not repeat that attempt.")
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
    files = d.get("files") if isinstance(d, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError('reply has no non-empty "files" list')
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not (
                f.get("delete") is True or isinstance(f.get("content"), str)):
            raise ValueError('every file needs a string "path" and either "content" or "delete": true')
    return str(d.get("explanation") or ""), files


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
              run: Callable[..., Any] = subprocess.run, worktree_root: str | None = None) -> FixResult:
    """Run up to `max_attempts` fix attempts for st.culprit. The attempts are also recorded on st.fix_attempts."""
    rep = st.culprit
    if not rep:
        raise ValueError("investigation has no culprit to fix")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    repo = Path(st.repo).resolve()
    head = _must(repo, "rev-parse", "--verify", f"{base or st.bad_sha or 'HEAD'}^{{commit}}").strip()
    branch = branch_name(rep.sha)
    res = FixResult(base=head, branch=branch)
    best: FixAttempt | None = None  # passing attempt, else the last one that produced a commit
    for n in range(1, max_attempts + 1):
        att = FixAttempt(branch=branch, attempt=n)
        res.attempts.append(att)
        with step_span("fix_attempt", attempt=n, culprit=rep.sha):
            try:
                prompt, oversize = build_prompt(str(repo), rep, head, res.attempts[:-1])
                att.explanation, edits = parse_reply(model.complete(SYSTEM, prompt))
                with worktree(repo, head, worktree_root) as wt:
                    att.files = apply_edits(wt, edits, oversize)
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
        best = att
        if att.passed:
            break
    st.fix_attempts = list(res.attempts)
    if best:
        _must(repo, "branch", "-f", branch, best.commit)
        res.patch = best.diff
        res.verifier_input = {"repo": str(repo), "commit": best.commit}
        res.passed = bool(best.passed)
    if open_pr and res.passed:
        try:
            res.pr_url = _open_pr(st, run)
        except (RuntimeError, ValueError) as e:
            res.pr_error = str(e)
    if out_dir:
        _write_files(st, res, Path(out_dir))
    st.metrics["fix"] = {"branch": res.branch, "passed": res.passed, "base": res.base,
                         "verifier_input": res.verifier_input, "pr_url": res.pr_url, "pr_error": res.pr_error}
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
