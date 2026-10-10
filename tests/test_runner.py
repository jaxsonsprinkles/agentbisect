import json
import subprocess
import threading
from pathlib import Path

import pytest

from agentbisect.runner import (CaseRequest, EvalInfraError, ModelPinError, RunResult, Runner, RunnerConfig,
                                parse_report, worktree)
from agentbisect.state import StateStore

PINS = {"agent_model": "claude-haiku-5-5", "judge_model": "claude-sonnet-5-5"}


class FakeExecutor:
    """script(sha, case, call_index) -> 'pass'|'fail'|'error:<reason>'|Exception ; records worktree paths used."""

    def __init__(self, script):
        self.script, self.calls, self.paths, self.envs = script, {}, [], []
        self.lock = threading.Lock()

    def run_case(self, req: CaseRequest):
        with self.lock:
            self.paths.append(req.workdir)
            self.envs.append(req.env)
            assert req.workdir.exists()
        out = []
        for _ in range(req.repeats):
            with self.lock:
                k = self.calls[(req.sha, req.case)] = self.calls.get((req.sha, req.case), 0) + 1
            o = self.script(req.sha, req.case, k)
            if isinstance(o, Exception):
                raise o
            st, _, why = o.partition(":")
            out.append(RunResult(req.case, st, trace_id=f"t-{req.sha[:6]}-{req.case}-{k}", reason=why, **(
                {} if st in ("skipped", "error") else {"agent_model": PINS["agent_model"],
                                                       "judge_model": PINS["judge_model"]})))
        return out

    def total(self):
        return sum(self.calls.values())


def make(repo, tmp_path, script, cases=("a", "b"), **kw):
    cfg = RunnerConfig(cases=list(cases), repeats=kw.pop("repeats", 2), install_cmd=kw.pop("install_cmd", None),
                       models=dict(PINS), backoff=0.0, **kw)
    ex = FakeExecutor(script)
    store = StateStore(tmp_path / "ab")
    runner = Runner(repo, cfg, store.cache(str(repo)), ex, sleep=lambda s: None)
    return runner, ex, store


def test_worktree_created_and_cleaned_up(repo):
    with worktree(repo, repo.shas[0]) as wt:
        assert (wt / "marker.txt").read_text() == "1"
        assert not (wt / "BROKEN").exists()
    assert not wt.exists()
    assert "wt" not in subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                                      text=True).stdout.split("\n", 1)[-1]


def test_worktree_cleanup_on_error(repo):
    with pytest.raises(RuntimeError):
        with worktree(repo, repo.shas[1]) as wt:
            raise RuntimeError("boom")
    assert not wt.exists()
    assert len(subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                              text=True).stdout.strip().splitlines()) == 1


def test_good_and_bad_verdicts(repo, tmp_path):
    r, ex, _ = make(repo, tmp_path, lambda sha, c, k: "pass" if sha == repo.shas[0] else "fail")
    good = r.evaluate_commit(repo.shas[0], "inv1")
    bad = r.evaluate_commit(repo.shas[2], "inv1")
    assert (good.verdict, good.pass_rate, good.executed) == ("good", 1.0, 4)
    assert (bad.verdict, bad.pass_rate, bad.failing_cases) == ("bad", 0.0, ["a", "b"])
    assert good.trace_ids and r.eval_rounds == 8
    assert ex.envs[0]["AGENTBISECT_COMMIT"] == repo.shas[0] and ex.envs[0]["AGENTBISECT_RUN_ID"] == "inv1"
    assert ex.envs[0]["AGENTBISECT_CASE"] in ("a", "b")
    assert not any(p.exists() for p in ex.paths)  # worktrees gone


def test_skip_on_build_failure_and_cached(repo, tmp_path):
    r, ex, _ = make(repo, tmp_path, lambda *a: "pass",
                    install_cmd=["sh", "-c", "test ! -f BROKEN || { echo dep conflict >&2; exit 1; }"])
    res = r.evaluate_commit(repo.shas[2], "i")
    assert res.verdict == "skip" and "install failed" in res.reason and "dep conflict" in res.reason
    assert ex.total() == 0
    assert r.evaluate_commit(repo.shas[2], "i").from_cache
    assert r.evaluate_commit(repo.shas[0], "i").verdict == "good"  # same install passes at c1


def test_cache_reuse_across_runners(repo, tmp_path):
    r1, ex1, store = make(repo, tmp_path, lambda *a: "pass")
    r1.evaluate_commit(repo.shas[0], "i")
    n = ex1.total()
    r2, ex2, _ = make(repo, tmp_path, lambda *a: "fail")  # would flip the result if it ran
    res = r2.evaluate_commit(repo.shas[0], "other-investigation")
    assert ex2.total() == 0 and res.verdict == "good" and res.from_cache and n == 4
    # different pinned models => no reuse
    r3, ex3, _ = make(repo, tmp_path, lambda *a: "pass")
    r3.cfg.models = {"agent_model": "claude-haiku-5-5", "judge_model": "claude-opus-5"}
    r3.executor.script = lambda *a: "pass"
    with pytest.raises(ModelPinError):  # fake still reports sonnet judge, now mismatching the pin
        r3.evaluate_commit(repo.shas[0], "i")
    assert ex3.total() > 0


def test_resume_after_simulated_crash_mid_step(repo, tmp_path):
    crash = {"on": True}

    def script(sha, case, k):
        if case == "b" and crash["on"]:
            raise KeyboardInterrupt  # simulated kill: not an EvalInfraError, escapes everything
        return "pass"

    r, ex, store = make(repo, tmp_path, script, workers=1)
    st = store.create(str(repo), bad_sha=repo.shas[2])
    store.set_status(st, "running")
    store.begin_step(st, "test_commit", sha=repo.shas[0])
    with pytest.raises(KeyboardInterrupt):
        r.evaluate_commit(repo.shas[0], st.id)
    assert not any(p.exists() for p in ex.paths)  # worktree cleaned even on crash
    # "new process"
    store2 = StateStore(tmp_path / "ab")
    st2 = store2.load(st.id)
    assert store2.interrupted(st2) and st2.in_flight["sha"] == repo.shas[0]
    crash["on"] = False
    r2, ex2, _ = make(repo, tmp_path, script, workers=1)
    redo = store2.resume(st2)
    assert redo["kind"] == "test_commit"
    res = r2.evaluate_commit(redo["sha"], st2.id)
    store2.end_step(st2, redo["sha"], res)
    assert ex2.calls.get((repo.shas[0], "a"), 0) == 0  # case a finished before the crash: reused from cache
    assert ex2.calls[(repo.shas[0], "b")] == 2
    final = StateStore(tmp_path / "ab").load(st.id)
    assert final.step == 1 and final.in_flight is None and final.results[repo.shas[0]].verdict == "good"


def test_flaky_escalates_to_cap_then_flaky_verdict(repo, tmp_path):
    # case a alternates pass/fail forever (50%): inside the band 0.5<r<0.85 -> escalate to cap, then 'flaky'
    r, ex, _ = make(repo, tmp_path, lambda sha, c, k: "pass" if c != "a" or k % 2 else "fail",
                    cases=("a", "b", "c"), repeats=2, max_repeats=8, good_min=0.95, bad_max=0.3)
    res = r.evaluate_commit(repo.shas[0], "i")
    assert res.verdict == "flaky" and res.flaky_cases == ["a"] and res.repeats == 8
    assert res.case_stats["a"]["passes"] + res.case_stats["a"]["fails"] == 8
    assert res.case_stats["b"]["passes"] == 2 and res.case_stats["c"]["passes"] == 2  # stable cases are not escalated
    assert "inside band" in res.reason


def test_flaky_resolves_after_escalation(repo, tmp_path):
    r, ex, _ = make(repo, tmp_path, lambda sha, c, k: "fail" if (c == "a" and k == 1) else "pass",
                    repeats=2, max_repeats=8, good_min=0.8, bad_max=0.3)
    res = r.evaluate_commit(repo.shas[0], "i")
    assert res.verdict == "good" and res.repeats == 4 and res.flaky_cases == ["a"]


def test_rate_limit_and_timeout_retried_with_backoff(repo, tmp_path):
    sleeps = []
    seq = iter([EvalInfraError("rate_limit", "429"), EvalInfraError("timeout", "slow")])

    def script(sha, c, k):
        if c == "a":
            e = next(seq, None)
            if e:
                return e
        return "pass"

    r, ex, _ = make(repo, tmp_path, script, cases=("a",))
    r.sleep = sleeps.append
    r.cfg.backoff = 1.5
    res = r.evaluate_commit(repo.shas[0], "i")
    assert res.verdict == "good" and sleeps == [1.5, 3.0]


def test_retries_are_bounded_then_error_is_no_verdict(repo, tmp_path):
    r, ex, _ = make(repo, tmp_path, lambda *a: EvalInfraError("rate_limit", "429"), max_retries=2)
    res = r.evaluate_commit(repo.shas[0], "i")
    assert res.verdict == "skip" and r.eval_rounds == 0  # nothing executed -> no verdict, not 'bad'


def test_error_results_with_rate_limit_text_retry_and_other_errors_excluded(repo, tmp_path):
    def script(sha, c, k):
        if c == "a":
            return "error:Anthropic 429 rate limit" if k == 1 else "pass"
        return "error:child crashed"

    r, ex, _ = make(repo, tmp_path, script, repeats=1)
    res = r.evaluate_commit(repo.shas[0], "i")
    assert res.verdict == "good" and res.executed == 1 and res.case_stats["b"]["errors"] == 1


def test_model_pin_mismatch_aborts(repo, tmp_path):
    class Drift(FakeExecutor):
        def run_case(self, req):
            return [RunResult(req.case, "pass", agent_model="claude-haiku-5-5", judge_model="claude-opus-5")]

    r, _, _ = make(repo, tmp_path, lambda *a: "pass")
    r.executor = Drift(None)
    with pytest.raises(ModelPinError):
        r.evaluate_commit(repo.shas[0], "i")


def test_skipped_cases_excluded_and_cached(repo, tmp_path):
    r, ex, _ = make(repo, tmp_path, lambda sha, c, k: "skipped:needs cancel_order" if c == "b" else "pass")
    res = r.evaluate_commit(repo.shas[0], "i")
    assert res.verdict == "good" and "b" not in res.case_stats
    calls = ex.total()
    assert r.evaluate_commit(repo.shas[0], "i").from_cache and ex.total() == calls


def test_parse_report_contract():
    rep = {"schema_version": 1, "agent_model": "claude-haiku-5-5", "judge_model": "claude-sonnet-5-5",
           "results": [{"case_id": "os1", "status": "pass", "trace_id": "abc", "reason": None},
                       {"case_id": "os2", "status": "error", "trace_id": None, "reason": "timeout"}]}
    out = parse_report(rep)
    assert [(r.case_id, r.status, r.trace_id, r.agent_model) for r in out] == [
        ("os1", "pass", "abc", "claude-haiku-5-5"), ("os2", "error", None, "claude-haiku-5-5")]
    with pytest.raises(EvalInfraError):
        parse_report({"schema_version": 2})


def test_subprocess_executor_end_to_end(tmp_path):
    from agentbisect.runner import SubprocessExecutor
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "__init__.py").write_text("")
    (tmp_path / "evals" / "__main__.py").write_text(
        "import json,os,sys\nprint(json.dumps({'schema_version':1,'agent_model':'claude-haiku-5-5',"
        "'judge_model':'claude-sonnet-5-5','results':[{'case_id':os.environ['AGENTBISECT_CASE'],'status':'pass',"
        "'trace_id':os.environ['AGENTBISECT_COMMIT']+sys.argv[-1]}]}))\n")
    import os, sys
    req = CaseRequest(tmp_path, "abc", "os1", 1, "rid", {**os.environ, "AGENTBISECT_CASE": "os1",
                                                           "AGENTBISECT_COMMIT": "abc"}, 30)
    out = SubprocessExecutor([sys.executable, "-m", "evals"]).run_case(req)
    assert out[0].case_id == "os1" and out[0].trace_id == "abcrid"


def test_json_tail_skips_sdk_log_lines_and_env_quiets_neatlogs(tmp_path):
    from agentbisect.runner import _json_tail, config_from_env, Runner, RunnerConfig
    assert json.loads(_json_tail('2026 - neatlogs - INFO - done\n{\n  "a": 1\n}\n')) == {"a": 1}
    assert json.loads(_json_tail('{"a": 1}')) == {"a": 1}
    r = Runner(tmp_path, RunnerConfig(cases=["x"]), None)
    assert r._env(None, None, None)["NEATLOGS_LOG_LEVEL"] == "ERROR"


def test_config_from_env(monkeypatch):
    from agentbisect.runner import config_from_env
    monkeypatch.setenv("AGENTBISECT_REPEATS", "2")
    monkeypatch.setenv("AGENTBISECT_WORKERS", "3")
    monkeypatch.setenv("AGENTBISECT_BASELINE", "/x/b.json")
    c = config_from_env(["a"])
    assert (c.repeats, c.workers, c.baseline) == (2, 3, "/x/b.json")
