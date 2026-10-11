"""Calibration (`agentbisect calibrate`), baseline validation and the refusal to run budget signatures without one."""
import json

import pytest

from agentbisect import bisect as B
from agentbisect import cli, runner as R
from agentbisect.baseline import Baseline, BaselineError, build_baseline, calibrate
from agentbisect.models import FailureSignature, RegressionSpec
from agentbisect.runner import Runner, RunnerConfig, RunResult
from agentbisect.state import StateStore
from test_bisect import PINS, hist  # noqa: F401
from test_budget import Exec

GOOD = "a" * 40


def data(**kw):
    return {"good_sha": GOOD, "agent_model": PINS["agent_model"], "cases": {"a": {"median_tokens": 100}}, **kw}


def test_validate_accepts_matching_sha_and_model_and_short_sha():
    b = Baseline(data())
    assert b.validate(GOOD, PINS["agent_model"]) is b
    assert Baseline(data(good_sha=GOOD[:12])).validate(GOOD, PINS["agent_model"])  # prefix either way round


@pytest.mark.parametrize("d, good, model, word", [
    (data(good_sha="b" * 40), GOOD, PINS["agent_model"], "calibrated at bbbbbbbbbb"),
    (data(), None, PINS["agent_model"], "good commit"),
    (data(agent_model="claude-opus-5"), GOOD, PINS["agent_model"], "agent model"),
    ({k: v for k, v in data().items() if k != "agent_model"}, GOOD, PINS["agent_model"], "agent model"),
    ({k: v for k, v in data().items() if k != "good_sha"}, GOOD, PINS["agent_model"], "no good_sha"),
    (data(cases={}), GOOD, PINS["agent_model"], "no per-case"),
    (data(good_sha="abc"), "abc", PINS["agent_model"], "calibrated at"),  # too short to trust a prefix match
])
def test_validate_rejects(d, good, model, word):
    with pytest.raises(BaselineError, match=word):
        Baseline(d).validate(good, model)


def test_load_errors_are_baseline_errors(tmp_path):
    with pytest.raises(BaselineError, match="cannot read"):
        Baseline.load(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(BaselineError, match="not valid JSON"):
        Baseline.load(tmp_path / "bad.json")


def test_build_baseline_uses_only_runs_whose_deterministic_checks_passed():
    e = {"detail": [{"status": "pass", "tokens": 100, "steps": 2, "fails": []},
                    {"status": "fail", "tokens": 120, "steps": 3, "fails": ["budget:tokens@x"]},  # budget miss ok
                    {"status": "fail", "tokens": 9999, "steps": 9, "fails": ["call_args:t.x"]},  # wrong behaviour
                    {"status": "pass", "tokens": 110, "steps": 2, "fails": []}]}
    out = build_baseline({"a": e, "b": {"detail": [{"status": "fail", "tokens": 5, "fails": ["judge"]}]}}, GOOD, PINS, 3,
                         {"c": "skipped by the target: no tool"})
    assert out["cases"]["a"] == {"median_tokens": 110, "median_steps": 2, "n": 3}
    assert set(out["excluded_cases"]) == {"b", "c"} and "b" not in out["cases"]
    assert (out["good_sha"], out["commit"], out["agent_model"], out["repeats"]) == (GOOD, GOOD, PINS["agent_model"], 3)
    assert out["date"] and out["created_at"] and out["token_factor"] == 1.5


def test_calibrate_runs_good_commit_three_repeats_without_baseline_and_writes_json(tmp_path, hist):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    ex = Exec(lambda idx, c, n: [100, 130, 110][n] + {"a": 0, "b": 1000}[c])
    r = Runner(repo, RunnerConfig(cases=["a", "b"], install_cmd=None, models=dict(PINS), workers=1,
                                  baseline="/should/not/be/used.json"), store.cache(str(repo)), ex, sleep=lambda s: None)
    out = tmp_path / "evidence" / "baseline.json"
    d = calibrate(r, shas[0], ["a", "b"], out)
    assert sum(n for _, n, _ in ex.reqs) == 6  # 3 repeats per case, one commit
    assert r.cfg.baseline == "/should/not/be/used.json" and r.cfg.repeats == 1  # config restored
    saved = json.loads(out.read_text())
    assert saved == d
    assert saved["cases"]["a"]["median_tokens"] == 110 and saved["cases"]["b"]["median_tokens"] == 1110
    assert saved["good_sha"] == shas[0] and saved["repeats"] == 3 and saved["agent_model"] == PINS["agent_model"]
    Baseline(saved).validate(shas[0], PINS["agent_model"])  # round trips through validation


def test_calibrate_refuses_to_write_when_nothing_measurable(tmp_path, hist):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")

    class Skipping:
        def run_case(self, req):
            return [RunResult(req.case, "skipped", reason="needs tool") for _ in range(req.repeats)]
    r = Runner(repo, RunnerConfig(cases=["a"], install_cmd=None, models=dict(PINS)), store.cache(str(repo)), Skipping())
    with pytest.raises(BaselineError):
        calibrate(r, shas[0], ["a"], tmp_path / "b.json")
    assert not (tmp_path / "b.json").exists()


def test_cli_calibrate_writes_file_and_prints_env_hint(tmp_path, hist, monkeypatch, capsys):
    repo, shas = hist
    monkeypatch.setattr(R, "SubprocessExecutor", lambda: Exec(lambda *a: 100))
    orig = R.config_from_env
    monkeypatch.setattr(R, "config_from_env", lambda cases, **kw: orig(cases, install_cmd=None, **kw))
    code = cli.main(["--state-dir", str(tmp_path / "ab"), "calibrate", "--repo", str(repo), "--good", shas[1],
                     "--cases", "a,b", "--out", str(tmp_path / "base.json")])
    out = capsys.readouterr().out
    assert code == 0 and "AGENTBISECT_BASELINE=" in out and "calibrated 2 cases" in out
    assert json.loads((tmp_path / "base.json").read_text())["good_sha"] == shas[1]


def test_cli_calibrate_bad_commit_is_a_clean_error(tmp_path, hist, capsys):
    repo, _ = hist
    assert cli.main(["--state-dir", str(tmp_path / "ab"), "calibrate", "--repo", str(repo), "--good", "nope",
                     "--cases", "a"]) == 1
    assert "error:" in capsys.readouterr().err


def _inv(tmp_path, hist, sig):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    spec = RegressionSpec(cases=["a", "b"], signature=sig)
    return store, store.create(str(repo), spec=spec, good_sha=shas[0], bad_sha=shas[-1]), shas


TOK = FailureSignature(key="budget:tokens@x", check="budget", cases=["a", "b"])


def test_investigation_refuses_budget_signature_without_baseline_and_never_calibrates(tmp_path, hist, monkeypatch):
    monkeypatch.delenv("AGENTBISECT_BASELINE", raising=False)
    store, st, shas = _inv(tmp_path, hist, TOK)
    ex = Exec(lambda *a: 100)
    runner = Runner(st.repo, RunnerConfig(cases=["a", "b"], install_cmd=None, models=dict(PINS)), store.cache(st.repo),
                    ex, sleep=lambda s: None)
    out = B.run_investigation(store, st, runner)
    st = store.load(st.id)
    assert out.kind == "failed" and "agentbisect calibrate" in out.message and st.status == "failed"
    assert ex.reqs == [] and not list((tmp_path / "ab").glob("baseline*"))  # nothing run, nothing calibrated


def test_behavioural_signature_does_not_need_a_baseline(tmp_path, hist):
    store, st, shas = _inv(tmp_path, hist, FailureSignature(key="call_args:t.x", check="call_args", cases=["a"]))
    ex = Exec(lambda *a: 100)
    runner = Runner(st.repo, RunnerConfig(cases=["a", "b"], install_cmd=None, models=dict(PINS)), store.cache(st.repo),
                    ex, sleep=lambda s: None, signature=st.spec.signature)
    out = B.run_investigation(store, st, runner)
    assert ex.reqs and "calibrate" not in out.message  # it ran (and stopped on its own terms: bad end passes)


def test_make_runner_rejects_a_baseline_from_another_commit_or_model(tmp_path, hist, monkeypatch):
    store, st, shas = _inv(tmp_path, hist, TOK)
    f = tmp_path / "base.json"
    monkeypatch.setenv("AGENTBISECT_BASELINE", str(f))
    f.write_text(json.dumps(data(good_sha=shas[3])))
    with pytest.raises(BaselineError, match="calibrated at"):
        B.make_runner(store, st)
    f.write_text(json.dumps(data(good_sha=shas[0], agent_model="claude-opus-5")))
    with pytest.raises(BaselineError, match="agent model"):
        B.make_runner(store, st)
    f.write_text(json.dumps(data(good_sha=shas[0])))
    assert B.make_runner(store, st).baseline is not None


def test_mismatched_baseline_fails_the_investigation_with_a_clear_error(tmp_path, hist, monkeypatch):
    store, st, shas = _inv(tmp_path, hist, TOK)
    f = tmp_path / "base.json"
    f.write_text(json.dumps(data(good_sha=shas[5])))
    monkeypatch.setenv("AGENTBISECT_BASELINE", str(f))
    monkeypatch.setattr(R.SubprocessExecutor, "run_case", lambda self, req: pytest.fail("must not evaluate"))
    out = B.run_investigation(store, st)
    assert out.kind == "failed" and "calibrated at" in out.message


def test_calibrate_pins_repeats_records_cap_and_lists_cases_that_never_ran(tmp_path, hist):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")

    class E(Exec):
        def run_case(self, req):
            if req.case == "dead":
                return [RunResult("dead", "error", reason="boom") for _ in range(req.repeats)]
            return super().run_case(req)
    ex = E(lambda idx, c, n: 100)
    r = Runner(repo, RunnerConfig(cases=["a", "dead"], install_cmd=None, models=dict(PINS), max_retries=0, workers=1),
               store.cache(str(repo)), ex, sleep=lambda s: None)
    d = calibrate(r, shas[0], ["a", "dead"], tmp_path / "b.json")
    assert d["cases"]["a"]["n"] == 3 and d["max_steps"] == 6 and "dead" in d["excluded_cases"]
    assert sum(n for c, n, _ in ex.reqs if c == "a") == 3


def test_steps_budget_signatures_are_refused_without_a_baseline_too(tmp_path, hist, monkeypatch):
    monkeypatch.delenv("AGENTBISECT_BASELINE", raising=False)
    store, st, shas = _inv(tmp_path, hist, FailureSignature(key="budget:steps@x", check="budget", cases=["a"]))
    runner = Runner(st.repo, RunnerConfig(cases=["a"], install_cmd=None, models=dict(PINS)), store.cache(st.repo),
                    Exec(lambda *a: 1), sleep=lambda s: None)
    assert B.run_investigation(store, st, runner).kind == "failed"
    assert store.load(st.id).metrics["no_baseline"] is True
    assert "no token baseline" in B.report(store.load(st.id), B.Outcome("failed", message="x"))


def test_each_sibling_budget_investigation_fails_cleanly_without_baseline(tmp_path, hist, monkeypatch):
    monkeypatch.delenv("AGENTBISECT_BASELINE", raising=False)
    store, st, shas = _inv(tmp_path, hist, TOK)
    ex = Exec(lambda *a: 100)
    r = Runner(st.repo, RunnerConfig(cases=["a", "b"], install_cmd=None, models=dict(PINS)), store.cache(st.repo), ex,
               sleep=lambda s: None)
    out = B.run_investigation(store, st, r)
    again = store.load(st.id)
    assert out.kind == "failed" and again.status == "failed" and "calibrate" in again.error
