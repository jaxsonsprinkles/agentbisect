"""Run the target repo's evals at a commit, in an isolated git worktree, with retries, caching and flaky handling.

Target contract (support-agent-demo evals/README.md): `python -m evals run --json --isolate --cases X
--repeats N --commit SHA --run-id ID` prints a schema_version 1 report on stdout. AgentBisect never reads the
target's ground truth; it only sees these results.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Protocol

from .models import CommitResult, FailureSignature
from .signature import failures_of_result, group_signatures
from .state import BUILD_CASE, VerdictCache
from .tracing import step_span

DEFAULT_AGENT_MODEL = "claude-haiku-5-5"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5-5"
RATE_LIMIT_RE = re.compile(r"rate.?limit|429|overloaded|too many requests|throttl", re.I)


class ModelPinError(RuntimeError):
    """A result was produced under different (or missing) pinned models: never mix, so abort."""


class EvalInfraError(RuntimeError):
    """Infrastructure failure running an eval (no verdict). kind: rate_limit | timeout | crash."""

    def __init__(self, kind: str, msg: str = ""):
        super().__init__(f"{kind}: {msg}")
        self.kind = kind


class BuildError(RuntimeError):
    pass


@dataclass
class RunResult:
    case_id: str
    status: str  # pass | fail | skipped | error
    trace_id: str | None = None
    agent_model: str | None = None
    judge_model: str | None = None
    reason: str = ""
    failures: list[dict] = field(default_factory=list)  # failing checks of this run (signature.failure_records)


@dataclass
class CaseRequest:
    workdir: Path
    sha: str
    case: str
    repeats: int
    run_id: str
    env: dict[str, str]
    timeout: float
    baseline: str | None = None


class EvalExecutor(Protocol):
    """Runs one case `repeats` times at a checked-out commit. Tests supply a fake; production uses SubprocessExecutor."""

    def run_case(self, req: CaseRequest) -> list[RunResult]: ...


def parse_report(report: dict) -> list[RunResult]:
    if report.get("schema_version") != 1:
        raise EvalInfraError("crash", f"unsupported schema_version {report.get('schema_version')!r}")
    return [RunResult(case_id=r["case_id"], status=r["status"], trace_id=r.get("trace_id"),
                      agent_model=r.get("agent_model") or report.get("agent_model"),
                      judge_model=r.get("judge_model") or report.get("judge_model"),
                      reason=r.get("reason") or "",
                      failures=failures_of_result(r) if r.get("status") == "fail" else [])
            for r in report.get("results", [])]


def _json_tail(out: str) -> str:
    """The JSON report starts at the first line that is exactly '{'; tolerate SDK log lines printed before it."""
    i = 0 if out.lstrip().startswith("{") else out.find("\n{\n") + 1
    return out[i:]


class SubprocessExecutor:
    def __init__(self, eval_cmd: list[str] | None = None):
        self.eval_cmd = eval_cmd or ["uv", "run", "python", "-m", "evals"]

    def run_case(self, req: CaseRequest) -> list[RunResult]:
        cmd = [*self.eval_cmd, "run", "--json", "--isolate", "--suite", "all", "--cases", req.case,
               "--repeats", str(req.repeats), "--commit", req.sha, "--run-id", req.run_id]
        if req.baseline:
            cmd += ["--baseline", req.baseline]
        try:
            p = subprocess.run(cmd, cwd=req.workdir, env=req.env, capture_output=True, text=True,
                               timeout=req.timeout)
        except subprocess.TimeoutExpired as e:
            raise EvalInfraError("timeout", f"{req.case} exceeded {req.timeout}s") from e
        try:
            return parse_report(json.loads(_json_tail(p.stdout)))
        except json.JSONDecodeError:
            tail = (p.stderr or p.stdout)[-500:]
            kind = "rate_limit" if RATE_LIMIT_RE.search(tail) else "crash"
            raise EvalInfraError(kind, f"exit {p.returncode}, no JSON report: {tail}")


@dataclass
class RunnerConfig:
    cases: list[str]
    repeats: int = 1  # R, initial repeats per case; more only when a result is borderline or judge-graded
    judge_cases: tuple[str, ...] = ()  # cases graded by an LLM rubric (noisy): always get judge_repeats
    judge_repeats: int = 3
    max_repeats: int = 12  # cap for escalation
    workers: int = 4
    timeout: float = 600.0
    install_cmd: list[str] | None = field(default_factory=lambda: ["uv", "sync"])
    install_timeout: float = 600.0
    max_retries: int = 3
    backoff: float = 2.0
    models: dict[str, str] = field(default_factory=lambda: {
        "agent_model": os.environ.get("AGENT_MODEL") or DEFAULT_AGENT_MODEL,
        "judge_model": os.environ.get("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL})
    # Confidence band: pass_rate >= good_min -> good, <= bad_max -> bad, else uncertain (escalate repeats).
    good_min: float = 0.85
    bad_max: float = 0.5
    classify: Callable[[float, int], str] | None = None  # hook: (pass_rate, n_runs) -> good|bad|uncertain
    env_file: str | None = None  # KEY=VALUE lines (API keys) injected into eval subprocesses; never logged
    worktree_root: str | None = None
    baseline: str | None = None  # absolute path to the target's evals baseline.json (kept outside the target repo)
    # Local Anthropic-compatible proxy (agentbisect llm-proxy): sets ANTHROPIC_BASE_URL + a dummy key in eval env.
    llm_proxy_url: str | None = field(default_factory=lambda: os.environ.get("AGENTBISECT_LLM_PROXY_URL") or None)


def config_from_env(cases: list[str], **kw) -> RunnerConfig:
    """RunnerConfig with operator overrides from the environment (budget control for live runs):
    AGENTBISECT_REPEATS, _MAX_REPEATS, _WORKERS, _ENV_FILE, _BASELINE."""
    env = os.environ
    for key, name, conv in (("repeats", "AGENTBISECT_REPEATS", int), ("max_repeats", "AGENTBISECT_MAX_REPEATS", int), ("judge_repeats", "AGENTBISECT_JUDGE_REPEATS", int),
                            ("workers", "AGENTBISECT_WORKERS", int), ("env_file", "AGENTBISECT_ENV_FILE", str),
                            ("baseline", "AGENTBISECT_BASELINE", str)):
        if env.get(name) and key not in kw:
            kw[key] = conv(env[name])
    return RunnerConfig(cases=cases, **kw)


def default_classify(cfg: RunnerConfig) -> Callable[[float, int], str]:
    def f(rate: float, n: int) -> str:
        return "good" if rate >= cfg.good_min else "bad" if rate <= cfg.bad_max else "uncertain"
    return f


@contextlib.contextmanager
def worktree(repo: Path, sha: str, root: str | None = None) -> Iterator[Path]:
    """Detached worktree at sha in a temp dir; always removed and pruned, even on error."""
    tmp = Path(tempfile.mkdtemp(prefix="agentbisect-wt-", dir=root))
    path = tmp / "wt"
    try:
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(path), sha],
                       capture_output=True, text=True, check=True)
        yield path
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)


def _load_env_file(path: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


class Runner:
    def __init__(self, repo: str | Path, cfg: RunnerConfig, cache: VerdictCache,
                 executor: EvalExecutor | None = None, sleep: Callable[[float], None] = time.sleep,
                 signature: FailureSignature | None = None):
        self.signature = signature  # when set, pass/fail means "this failing check does not occur"
        self.repo = Path(repo).resolve()
        self.cfg = cfg
        self.cache = cache
        self.executor = executor or SubprocessExecutor()
        self.sleep = sleep
        self.classify = cfg.classify or default_classify(cfg)
        self.eval_rounds = 0  # individual eval runs executed (not cache hits): the smart-vs-plain metric
        self._lock = threading.Lock()

    # -- install --

    def _install(self, wt: Path) -> None:
        if not self.cfg.install_cmd:
            return
        try:
            p = subprocess.run(self.cfg.install_cmd, cwd=wt, capture_output=True, text=True,
                               timeout=self.cfg.install_timeout, env=self._env(None, None, None))
        except subprocess.TimeoutExpired:
            raise BuildError(f"install timed out after {self.cfg.install_timeout}s")
        except OSError as e:
            raise BuildError(f"install could not start: {e}")
        if p.returncode != 0:
            raise BuildError(f"install failed (exit {p.returncode}): {(p.stderr or p.stdout)[-300:].strip()}")

    def _env(self, sha: str | None, run_id: str | None, case: str | None, salt: str | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
        env.update(_load_env_file(self.cfg.env_file))
        # The neatlogs SDK logs "shutdown complete" to STDOUT at INFO, which corrupts the harness's JSON report.
        env.setdefault("NEATLOGS_LOG_LEVEL", "ERROR")
        if self.cfg.llm_proxy_url:
            # the salt rides in the URL path (not the prompt): the proxy keys its response cache on it, so repeat i of
            # a case at a commit is its own sample, and replaying the same (commit, case, repeat) hits the cache
            env["ANTHROPIC_BASE_URL"] = self.cfg.llm_proxy_url.rstrip("/") + (f"/salt/{salt}" if salt else "")
            env["ANTHROPIC_API_KEY"] = "agentbisect-proxy"
        for k, v in (("AGENTBISECT_COMMIT", sha), ("AGENTBISECT_RUN_ID", run_id), ("AGENTBISECT_CASE", case)):
            if v:
                env[k] = v
        return env

    # -- one case, with retries --

    def _run_case(self, wt: Path, sha: str, case: str, n: int, run_id: str, base: int = 0) -> list[RunResult]:
        """Run `case` n more times (repeat indices base..). Rate limits and timeouts retry (only the missing runs)
        with exponential backoff; runs still failing after max_retries become 'error' results, i.e. no verdict
        rather than a bad verdict. Through the LLM proxy every repeat is its own request with its own salt."""
        results: list[RunResult] = []
        last = "unknown"
        for attempt in range(self.cfg.max_retries + 1):
            remaining = n - len(results)
            for b in ([1] * remaining if self.cfg.llm_proxy_url else [remaining]):
                done = sum(r.status in ("pass", "fail") for r in results)
                errs = sum(r.status == "error" for r in results)  # an errored run left cached replies: new salt
                salt = (f"{sha[:12]}.{hashlib.sha1(case.encode()).hexdigest()[:10]}.r{base + done}"
                        + (f".e{errs}" if errs else "")) if self.cfg.llm_proxy_url else None
                req = CaseRequest(wt, sha, case, b, run_id, self._env(sha, run_id, case, salt), self.cfg.timeout,
                                  self.cfg.baseline)
                try:
                    got = self.executor.run_case(req)
                except EvalInfraError as e:
                    last = str(e)
                    break
                self._check_pins(got)
                retry = [r for r in got if r.status == "error" and RATE_LIMIT_RE.search(r.reason)]
                results += [r for r in got if r not in retry]
                if retry:
                    last = f"rate_limit: {retry[0].reason}"
                    break  # back off before the next attempt instead of firing the rest back to back
            if len(results) >= n:
                return results
            if attempt < self.cfg.max_retries:
                self.sleep(self.cfg.backoff * (2 ** attempt))
        return results + [RunResult(case, "error", reason=f"{last} (gave up after {self.cfg.max_retries + 1} attempts)")
                          for _ in range(n - len(results))]

    def _check_pins(self, results: list[RunResult]) -> None:
        for r in results:
            if r.status in ("skipped", "error"):
                continue
            for key in ("agent_model", "judge_model"):
                got = getattr(r, key)
                if got != self.cfg.models[key]:
                    raise ModelPinError(
                        f"{r.case_id}: {key} {got!r} != pinned {self.cfg.models[key]!r}; refusing to mix models")

    # -- one commit --

    def evaluate_commit(self, sha: str, run_id: str, cases: list[str] | None = None) -> CommitResult:
        """K cases x R repeats at `sha` in one worktree, `workers` cases in parallel. While the verdict is inside
        the confidence band, cases that disagree with themselves (or fail, at one repeat) get doubled repeats up to
        max_repeats; judge-graded cases always run `judge_repeats`; if it is
        still uncertain the verdict is 'flaky'. Build/worktree failure => 'skip' (cached). Finished cases are
        cached immediately, so a crash mid-step loses at most the in-flight cases."""
        sig = self.signature
        cases = list(cases or (sig.cases if sig and sig.cases else None) or self.cfg.cases)
        models = self.cfg.models
        cached_skip = self.cache.get(sha, BUILD_CASE, models)
        if cached_skip:
            return CommitResult("skip", reason=cached_skip["skip"], from_cache=True)

        def have(case: str) -> dict:
            return self.cache.get(sha, case, models) or {}

        def need(case: str, target: int) -> int:
            e = have(case)  # executed runs, whatever failed in them
            return 0 if "skip" in e else max(0, target - e.get("runs", 0))

        errors: dict[str, int] = {c: 0 for c in cases}
        wt_ctx = None
        wt: Path | None = None
        ran = False

        def work(cn: tuple[str, int]) -> None:
            case, n = cn
            base = have(case).get("runs", 0)
            results = self._run_case(wt, sha, case, n, run_id, base)
            p = sum(r.status == "pass" for r in results)
            f = sum(r.status == "fail" for r in results)
            errors[case] += sum(r.status == "error" for r in results)
            if results and all(r.status == "skipped" for r in results):
                self.cache.set_case_skip(sha, case, models, results[0].reason or "skipped by target")
                return
            with self._lock:
                self.eval_rounds += p + f
            k = base
            for r in results:
                if r.status not in ("pass", "fail"):
                    continue
                recs = r.failures or ([{"key": "case_fail", "check": "case_fail", "reason": r.reason}]
                                      if r.status == "fail" else [])  # a failing run always has a cause key
                self.cache.add_run(sha, case, models, k, r.status, r.trace_id, {x["key"]: x for x in recs})
                k += 1

        try:
            with step_span("evaluate_commit", sha=sha, run_id=run_id):
                active, target = cases, self.cfg.repeats
                while True:
                    todo = [(c, need(c, max(target, self.cfg.judge_repeats) if c in self.cfg.judge_cases else target))
                            for c in active]
                    todo = [t for t in todo if t[1] > 0]
                    if todo and wt_ctx is None:
                        wt_ctx = worktree(self.repo, sha, self.cfg.worktree_root)
                        try:
                            wt = wt_ctx.__enter__()
                            self._install(wt)
                        except (BuildError, subprocess.CalledProcessError) as e:
                            msg = str(e) if isinstance(e, BuildError) else \
                                f"git worktree failed: {(e.stderr or '').strip()[-200:]}"
                            self.cache.set_skip(sha, models, msg)
                            return CommitResult("skip", reason=msg)
                    if todo:
                        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as pool:
                            list(pool.map(work, todo))
                        ran = True
                    stats = self._tally(sha, cases, errors)
                    passes = sum(t["passes"] for t in stats.values())
                    fails = sum(t["fails"] for t in stats.values())
                    if passes + fails == 0 and self.signature and not any(errors.values()) and all(
                            "skip" in (self.cache.get(sha, c, models) or {}) for c in cases):
                        # the signature's cases do not exist at this commit (e.g. they need a tool added later):
                        # the failure cannot occur here, so it is vacuously good, not an unusable commit
                        return CommitResult("good", pass_rate=1.0, executed=0, case_stats={}, from_cache=not ran,
                                            reason="signature cases not applicable at this commit")
                    if passes + fails == 0:
                        return CommitResult("skip", reason="no executed runs (all errored or skipped)",
                                            case_stats=stats)
                    rate = passes / (passes + fails)
                    band = self.classify(rate, passes + fails)
                    # borderline: re-run the cases that disagree with themselves; with a single sample per case there
                    # is no disagreement to see yet, so re-run every failing case
                    flaky = [c for c, t in stats.items() if t["passes"] and t["fails"]]
                    again = flaky or ([c for c, t in stats.items() if t["fails"]] if target <= 1 else [])
                    if band != "uncertain" or not again or target >= self.cfg.max_repeats:
                        break
                    target, active = min(self.cfg.max_repeats, target * 2), again
        finally:
            if wt_ctx is not None:
                wt_ctx.__exit__(None, None, None)

        traces = [t for c in stats for t in (have(c).get("trace_ids") or [])]
        verdict = {"good": "good", "bad": "bad"}.get(band, "flaky")
        return CommitResult(
            verdict, pass_rate=rate, trace_ids=traces, executed=passes + fails, repeats=target,
            flaky_cases=flaky, failing_cases=[c for c, t in stats.items() if t["fails"] and not t["passes"]],
            case_stats=stats, from_cache=not ran,
            failure_signatures=self._signatures(sha, cases),
            reason=(f"pass_rate {rate:.2f} inside band ({self.cfg.bad_max}, {self.cfg.good_min}) after "
                    f"{target} repeats" if verdict == "flaky" else ""))

    def _tally(self, sha: str, cases: list[str], errors: dict[str, int]) -> dict[str, dict[str, int]]:
        """Per case passes/fails/errors. With a signature a run only counts as failed when that check failed in
        it; any other failure (another regression's budget miss, say) is a pass for this signature."""
        out = {}
        for c in cases:
            e = self.cache.get(sha, c, self.cfg.models) or {}
            if "skip" in e:
                continue
            runs = e.get("passes", 0) + e.get("fails", 0)
            fails = e.get("fails", 0)
            if self.signature:
                key = self.signature.key  # a bare `budget:tokens` also covers the per-category `budget:tokens@cat`
                cf = e.get("check_fails") or {}
                fails = min(runs, sum(n for k, n in cf.items() if k == key or k.startswith(key + "@")))
            out[c] = {"passes": runs - fails, "fails": fails, "errors": errors.get(c, 0)}
        return out

    def signatures_at(self, sha: str, cases: list[str]) -> list[dict]:
        return self._signatures(sha, cases)

    def _signatures(self, sha: str, cases: list[str]) -> list[dict]:
        """Every failing check seen at `sha` over `cases`, grouped by key (see signature.py)."""
        infos: dict[str, dict] = {}
        runs: dict[str, int] = {}
        fails: dict[str, dict[str, int]] = {}
        for c in cases:
            e = self.cache.get(sha, c, self.cfg.models) or {}
            if "skip" in e:
                continue
            runs[c] = e.get("passes", 0) + e.get("fails", 0)
            fails[c] = dict(e.get("check_fails") or {})
            for k, rec in (e.get("check_info") or {}).items():
                infos.setdefault(k, rec)
        return group_signatures(infos, runs, fails)
