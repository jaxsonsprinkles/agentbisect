import json
import subprocess
from pathlib import Path

import pytest

from agentbisect import bisect as B
from agentbisect import cli
from agentbisect.intake import IntakeResult
from agentbisect.models import CommitResult, RegressionSpec
from agentbisect.runner import CaseRequest, RunResult, Runner, RunnerConfig
from agentbisect.state import StateStore
from conftest import git

PINS = {"agent_model": "claude-haiku-5-5", "judge_model": "claude-sonnet-5-5"}


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the engine may swallow it, like a kill -9."""


@pytest.fixture
def hist(tmp_path):
    """14 linear commits; marker.txt = index. Commit 4 also adds BROKEN (install fails there)."""
    r = tmp_path / "target"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    shas = []
    for i in range(14):
        (r / "marker.txt").write_text(str(i))
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", f"c{i}")
        shas.append(git(r, "rev-parse", "HEAD"))
    return r, shas


class FakeExec:
    """behaviour(index, case, k) -> 'pass'|'fail'|'error:..' ; index read from the checked-out marker.txt."""

    def __init__(self, behaviour, crash_after=None):
        self.behaviour, self.calls, self.crash_after, self.n = behaviour, {}, crash_after, 0
        self.tested = []

    def run_case(self, req: CaseRequest):
        idx = int((req.workdir / "marker.txt").read_text())
        out = []
        for _ in range(req.repeats):
            self.n += 1
            if self.crash_after is not None and self.n > self.crash_after:
                raise SimulatedCrash()
            k = self.calls[(idx, req.case)] = self.calls.get((idx, req.case), 0) + 1
            st, _, why = self.behaviour(idx, req.case, k).partition(":")
            out.append(RunResult(req.case, st, trace_id=f"t{idx}-{k}", reason=why,
                                 agent_model=PINS["agent_model"], judge_model=PINS["judge_model"]))
        self.tested.append(idx)
        return out


def culprit_at(c):
    return lambda idx, case, k: "fail" if idx >= c else "pass"


def setup(tmp_path, hist, executor, **cfg):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    runner = Runner(repo, RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=cfg.pop("install_cmd", None),
                                       models=dict(PINS), backoff=0.0, **cfg),
                    store.cache(str(repo)), executor, sleep=lambda s: None)
    st = store.create(str(repo), spec=RegressionSpec(cases=["a", "b"]), good_sha=shas[0], bad_sha=shas[-1])
    return store, runner, st


def run(store, st, runner):
    return B.run_investigation(store, st, runner)


def test_finds_culprit_in_log2_steps(tmp_path, hist):
    ex = FakeExec(culprit_at(9))
    store, runner, st = setup(tmp_path, hist, ex)
    out = run(store, st, runner)
    assert out.kind == "culprit" and out.sha == hist[1][9]
    st = store.load(st.id)
    assert st.status == "culprit_found" and st.culprit.sha == hist[1][9] and st.culprit.confidence == "high"
    assert st.in_flight is None and not st.metrics.get("warnings")
    # 2 end confirmations + <= ceil(log2 12)=4 bisect steps + 1 monotonic probe
    assert st.step <= 7
    assert st.metrics["eval_rounds"] == runner.eval_rounds > 0 and st.metrics["cost_usd"] > 0
    assert st.plan.mode == "plain"


def test_every_culprit_position_is_found(tmp_path, hist):
    for c in range(1, 14):
        ex = FakeExec(culprit_at(c))
        store, runner, st = setup(tmp_path / str(c), hist, ex)
        out = run(store, st, runner)
        assert out.sha == hist[1][c], c


def test_skipped_neighbour_still_resolves(tmp_path, hist):
    # commits 6 and 7 (the first midpoints) fail to install; culprit is 9
    store, runner, st = setup(tmp_path, hist, FakeExec(culprit_at(9)),
                              install_cmd=["sh", "-c", 'test "$(cat marker.txt)" != 6 && test "$(cat marker.txt)" != 7'])
    out = run(store, st, runner)
    assert out.kind == "culprit" and out.sha == hist[1][9]
    st = store.load(st.id)
    assert st.results[hist[1][6]].verdict == "skip" and st.culprit.confidence == "medium"


def test_skipped_commits_adjacent_to_culprit_are_reported(tmp_path, hist):
    # culprit is 7 but 6 (its parent) cannot build: culprit is in {6, 7}
    store, runner, st = setup(tmp_path, hist, FakeExec(culprit_at(7)),
                              install_cmd=["sh", "-c", 'test "$(cat marker.txt)" != 6'])
    out = run(store, st, runner)
    assert out.kind == "unresolved"
    assert out.candidates == [hist[1][6], hist[1][7]]
    assert "culprit is within these skipped" in out.message
    assert B.EXIT_UNRESOLVED == 4 and store.load(st.id).metrics["ambiguous"] == out.candidates


def test_flaky_commit_is_inconclusive_with_lower_confidence(tmp_path, hist):
    def beh(idx, case, k):
        if idx == 6:
            return "pass" if (case == "b" or k % 2 == 0) else "fail"  # 0.75 pass rate forever: inside the band
        return culprit_at(9)(idx, case, k)
    store, runner, st = setup(tmp_path, hist, FakeExec(beh), max_repeats=4)
    out = run(store, st, runner)
    assert out.sha == hist[1][9]
    st = store.load(st.id)
    assert st.results[hist[1][6]].verdict == "flaky"
    assert st.culprit.confidence == "medium"
    assert st.metrics["confidence_band"]["good_pass_rate"] == 1.0


def test_crash_mid_bisect_then_resume(tmp_path, hist):
    ex = FakeExec(culprit_at(9), crash_after=14)
    store, runner, st = setup(tmp_path, hist, ex)
    with pytest.raises(SimulatedCrash):
        run(store, st, runner)
    disk = StateStore(tmp_path / "ab").load(st.id)  # a fresh process reads the file
    assert disk.status == "running" and disk.in_flight is not None and disk.step >= 2
    done_before = set(disk.results)
    ex2 = FakeExec(culprit_at(9))
    runner2 = Runner(hist[0], RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS)),
                     store.cache(str(hist[0])), ex2)
    redo = store.resume(disk)
    assert redo["kind"] in ("bisect", "confirm_bad", "confirm_good", "monotonic_probe")
    assert B.resume_investigation(store, disk, runner2) == 0
    final = store.load(st.id)
    assert final.status == "culprit_found" and final.culprit.sha == hist[1][9] and final.in_flight is None
    assert not (set(ex2.tested) & {hist[1].index(s) for s in done_before})  # finished commits are not redone


def test_non_monotonic_verdicts_warn(tmp_path, hist):
    # regression from 7 on, but 10 passes again: the monotonic probe finds a good after a bad
    beh = lambda idx, case, k: "pass" if idx < 7 or idx == 10 else "fail"
    store, runner, st = setup(tmp_path, hist, FakeExec(beh))
    out = run(store, st, runner)
    st = store.load(st.id)
    assert out.sha == hist[1][7]
    assert any("multiple regressions likely" in w for w in st.metrics["warnings"])
    assert st.culprit.confidence == "low"
    assert "multiple regressions likely" in B.report(st, out)


def test_narrow_flags_good_after_bad():
    h = list("abcdef")
    r = lambda v: CommitResult(v, pass_rate=1.0 if v == "good" else 0.0)
    n = B.narrow(h, {"a": r("good"), "c": r("bad"), "e": r("good"), "f": r("bad")})
    assert n.non_monotonic and (n.lo, n.hi) == (0, 2) and n.untested == [1]


def test_bad_end_does_not_reproduce(tmp_path, hist):
    store, runner, st = setup(tmp_path, hist, FakeExec(lambda *a: "pass"))
    out = run(store, st, runner)
    assert out.kind == "failed" and "bad end does not reproduce" in out.message
    st = store.load(st.id)
    assert st.status == "failed" and "bad end does not reproduce" in st.error and st.culprit is None


def test_good_end_not_good(tmp_path, hist):
    store, runner, st = setup(tmp_path, hist, FakeExec(lambda *a: "fail"))
    assert "good end is not good" in run(store, st, runner).message


def test_missing_good_sha(tmp_path, hist):
    store, runner, st = setup(tmp_path, hist, FakeExec(culprit_at(3)))
    st.good_sha = None
    assert "--good" in run(store, st, runner).message


def test_cache_reuse_across_investigations(tmp_path, hist):
    ex1 = FakeExec(culprit_at(6))
    store, runner, st = setup(tmp_path, hist, ex1)
    run(store, st, runner)
    first_rounds = store.load(st.id).metrics["eval_rounds"]
    assert first_rounds > 0
    ex2 = FakeExec(lambda *a: pytest.fail("cache should have answered"))
    runner2 = Runner(hist[0], RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS)),
                     store.cache(str(hist[0])), ex2)
    st2 = store.create(str(hist[0]), spec=RegressionSpec(cases=["a", "b"]), good_sha=hist[1][0], bad_sha=hist[1][-1])
    out = run(store, st2, runner2)
    st2 = store.load(st2.id)
    assert out.sha == hist[1][6] and st2.metrics["eval_rounds"] == 0 and st2.metrics["cache_hits"] > 0


def test_custom_strategy_plugs_in(tmp_path, hist):
    class Linear:  # scans from the oldest commit: proves the Strategy seam
        def __init__(self, h): self.h, self.seen = h, []
        def next_commit(self, st):
            n = B.narrow(self.h, st.results)
            return self.h[n.untested[0]] if n.untested else None
        def update(self, st, sha, res): self.seen.append(sha)
    repo, shas = hist
    store, runner, st = setup(tmp_path, hist, FakeExec(culprit_at(4)))
    s = Linear(B.linear_history(str(repo), shas[0], shas[-1]))
    out = B.run_investigation(store, st, runner, s)
    assert out.sha == shas[4] and s.seen


def test_cli_investigate_and_resume(tmp_path, hist, monkeypatch, capsys):
    repo, shas = hist
    spec = RegressionSpec(cases=["a", "b"])
    monkeypatch.setattr(cli, "run_intake", lambda a: IntakeResult(spec, None, None, []))
    ex = FakeExec(culprit_at(5))
    monkeypatch.setattr(B, "make_runner", lambda store, st: Runner(
        st.repo, RunnerConfig(cases=spec.cases, repeats=2, install_cmd=None, models=dict(PINS)),
        store.cache(st.repo), ex))
    sd = str(tmp_path / "ab")
    rc = cli.main(["--state-dir", sd, "investigate", "--trace", "t", "--repo", str(repo), "--strategy", "plain",
                   "--good", shas[0]])
    assert rc == 0 and shas[5][:8] in capsys.readouterr().out
    st = StateStore(sd).list()[0]
    assert st.culprit.sha == shas[5]
    assert cli.main(["--state-dir", sd, "resume", st.id]) == 0
