"""Budget signatures: repeats per case, median rule vs the saved baseline, configurable repeats."""
import hashlib
import threading

from agentbisect import bisect as B
from agentbisect.baseline import Baseline, budget_kind
from agentbisect.models import FailureSignature, RegressionSpec
from agentbisect.runner import Runner, RunnerConfig, RunResult, config_from_env, parse_report
from agentbisect.state import StateStore
from test_bisect import PINS, hist  # noqa: F401

BASE = Baseline({"token_factor": 1.5, "max_steps": 6, "cases": {c: {"median_tokens": 100, "median_steps": 2, "n": 3}
                                                                  for c in "abc"}})
TOK = FailureSignature(key="budget:tokens@x", check="budget", detail="tokens@x", cases=["a", "b"])
BEH = FailureSignature(key="call_args:t.x", check="call_args", detail="t.x", cases=["a", "b"])


class Exec:
    """tokens(idx, case, repeat_no) -> int; records (case, repeats, url) per call; call_args fails for case 'a' at idx>=1."""

    def __init__(self, tokens):
        self.tokens, self.reqs, self.lock, self.seen = tokens, [], threading.Lock(), {}

    def run_case(self, req):
        idx = int((req.workdir / "marker.txt").read_text())
        out = []
        with self.lock:
            self.reqs.append((req.case, req.repeats, req.env.get("ANTHROPIC_BASE_URL")))
            for _ in range(req.repeats):
                n = self.seen[(idx, req.case)] = self.seen.get((idx, req.case), -1) + 1
                tok = self.tokens(idx, req.case, n)
                over = tok > 150  # the harness's own per-run flag, limit 1.5 x 100
                fails = [{"key": "budget:tokens@x", "check": "budget"}] if over else []
                out.append(RunResult(req.case, "fail" if over else "pass", trace_id=f"t{idx}{req.case}{n}",
                                     agent_model=PINS["agent_model"], judge_model=PINS["judge_model"],
                                     failures=fails, tokens=tok, steps=2, max_steps=6))
        return out


def mk(tmp_path, hist, tokens, sig=TOK, cases=("a", "b"), **cfg):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    ex = Exec(tokens)
    r = Runner(repo, RunnerConfig(cases=list(cases), install_cmd=None, models=dict(PINS), backoff=0.0, workers=1, **cfg),
               store.cache(str(repo)), ex, sleep=lambda s: None, signature=sig)
    r.baseline = BASE
    return r, ex, shas


def test_budget_kind():
    assert budget_kind("budget:tokens@order_status") == "tokens" and budget_kind("budget:steps") == "steps"
    assert budget_kind("call_args:t.x") is None and budget_kind(None) is None


def test_parse_report_carries_budget_metrics():
    rep = {"schema_version": 1, "agent_model": "m", "judge_model": "j", "results": [
        {"case_id": "a", "status": "pass", "tokens": 321, "steps": 3, "budget": {"steps": 3, "max_steps": 6}}]}
    r = parse_report(rep)[0]
    assert (r.tokens, r.steps, r.max_steps) == (321, 3, 6)


def test_budget_signature_runs_three_repeats_per_case(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100)
    res = r.evaluate_commit(shas[0], "x")
    assert sum(n for _, n, _ in ex.reqs) == 6 and res.repeats == 3 and res.verdict == "good"
    assert r.eval_rounds == 6


def test_behavioural_signature_stays_at_one_repeat(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100, sig=BEH)
    r.evaluate_commit(shas[0], "x")
    assert sum(n for _, n, _ in ex.reqs) == 2


def test_only_budget_cases_run_at_three_x(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100, sig=None)
    r.evaluate_commit(shas[0], "x", budget_cases=["b"])
    per = {}
    for c, n, _ in ex.reqs:
        per[c] = per.get(c, 0) + n
    assert per == {"a": 1, "b": 3}


def test_budget_repeats_configurable(tmp_path, hist, monkeypatch):
    monkeypatch.delenv("AGENTBISECT_REPEATS", raising=False)
    monkeypatch.delenv("AGENTBISECT_BUDGET_REPEATS", raising=False)
    c = config_from_env(["a"])
    assert c.budget_repeats == 3 and c.repeats == 1
    monkeypatch.setenv("AGENTBISECT_BUDGET_REPEATS", "5")
    assert config_from_env(["a"]).budget_repeats == 5 and config_from_env(["a"]).repeats == 1
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100, budget_repeats=5)
    r.evaluate_commit(shas[0], "x")
    assert sum(n for _, n, _ in ex.reqs) == 10


def test_median_not_mean_decides_the_case(tmp_path, hist):
    # case a: one wild repeat (900) among two normal ones -> median 100, within 150: good although a run was flagged
    # case b: two over-limit repeats and one normal -> median 200, over the limit
    vals = {"a": [100, 900, 100], "b": [100, 200, 200]}
    r, ex, shas = mk(tmp_path, hist, lambda idx, c, n: vals[c][n])
    res = r.evaluate_commit(shas[0], "x")
    assert res.case_stats["a"] == {"passes": 3, "fails": 0, "errors": 0}
    assert res.case_stats["b"]["fails"] == 3
    assert res.pass_rate == 0.5 and res.verdict == "bad"
    b = res.budget
    assert b["kind"] == "tokens" and b["cases"]["a"]["median"] == 100 and b["cases"]["b"]["median"] == 200
    assert (b["within"], b["total"], b["median"], b["baseline_median"], b["ratio"]) == (1, 2, 150.0, 100.0, 1.5)


def test_median_applies_even_if_no_run_was_flagged_by_the_harness(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda *a: 140)  # each run under the harness limit...
    r.baseline = Baseline({"token_factor": 1.2, "cases": {c: {"median_tokens": 100} for c in "ab"}})  # ...but ours is 120
    assert r.evaluate_commit(shas[0], "x").verdict == "bad"


def test_repeats_cached_per_commit_case_repeat_and_salted(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100, cases=("a",), sig=FailureSignature(key="budget:tokens@x", check="budget", cases=["a"]),
                  llm_proxy_url="http://127.0.0.1:9")
    r.evaluate_commit(shas[0], "x")
    urls = [u for _, _, u in ex.reqs]
    tag = hashlib.sha1(b"a").hexdigest()[:10]
    assert len(set(urls)) == 3 and urls[1].endswith(f".{tag}.r1")
    n = len(ex.reqs)
    r.evaluate_commit(shas[0], "y")  # replay: all three repeats come from the cache
    assert len(ex.reqs) == n


def test_no_baseline_falls_back_to_harness_flags(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda idx, c, n: 200)
    r.baseline = None
    assert r.evaluate_commit(shas[0], "x").verdict == "bad"  # steps/tokens flags set by the harness are honoured


def test_steps_budget_uses_the_run_reported_cap(tmp_path, hist):
    sig = FailureSignature(key="budget:steps@x", check="budget", cases=["a"])
    repo, shas = hist
    store = StateStore(tmp_path / "ab")

    class E(Exec):
        pass
    ex = E(lambda *a: 100)
    orig = ex.run_case

    def run(req):
        out = orig(req)
        for i, x in enumerate(out):
            x.steps = [3, 9, 9][i % 3]  # median 9 > cap 6
        return out
    ex.run_case = run
    r = Runner(repo, RunnerConfig(cases=["a"], install_cmd=None, models=dict(PINS), workers=1), store.cache(str(repo)),
               ex, sleep=lambda s: None, signature=sig)
    assert r.evaluate_commit(shas[0], "x").verdict == "bad"


def test_investigation_finds_budget_culprit_by_median_and_confirms_with_repeats(tmp_path, hist):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    # token regression from commit 6 on in case a; one noisy over-limit repeat before it must not matter
    ex = Exec(lambda idx, c, n: 200 if (c == "a" and idx >= 6) or (c == "a" and idx == 2 and n == 0) else 100)
    runner = Runner(repo, RunnerConfig(cases=["a", "b"], install_cmd=None, models=dict(PINS), backoff=0.0, workers=1),
                    store.cache(str(repo)), ex, sleep=lambda s: None)
    runner.baseline = BASE
    st = store.create(str(repo), spec=RegressionSpec(cases=["a", "b"], bad_case="a"), good_sha=shas[0], bad_sha=shas[-1])
    out = B.run_investigation(store, st, runner)
    st = store.load(st.id)
    assert out.kind == "culprit" and out.sha == shas[6]
    assert st.spec.signature.check == "budget"
    # the bad end ran case a at 1 repeat for discovery, then 3 for the confirmation/bisect
    assert any(n == 3 for c, n, _ in ex.reqs if c == "a")


def test_errored_repeat_is_left_out_of_the_median(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100, cases=("a",), sig=FailureSignature(
        key="budget:tokens@x", check="budget", cases=["a"]))
    orig = ex.run_case

    def run(req):
        out = orig(req)
        if not hasattr(run, "done"):  # first call: one repeat errors (no tokens, no verdict)
            run.done = True
            out[0] = RunResult("a", "error", reason="boom")
        return out
    ex.run_case = run
    res = r.evaluate_commit(shas[0], "x")
    assert res.budget["cases"]["a"]["runs"] == 2 and res.case_stats["a"]["errors"] == 1 and res.verdict == "good"


def test_budget_cases_never_escalate_even_when_borderline(tmp_path, hist):
    # 2 of 3 cases over budget -> rate .33 (<= bad_max .5 => bad); make it borderline with good_min .9/bad_max .3
    r, ex, shas = mk(tmp_path, hist, lambda idx, c, n: 200 if c == "a" else 100,
                     sig=FailureSignature(key="budget:tokens@x", check="budget", cases=["a", "b", "c"]),
                     cases=("a", "b", "c"), good_min=0.9, bad_max=0.3, max_repeats=12)
    res = r.evaluate_commit(shas[0], "x")
    assert res.verdict == "flaky" and res.repeats == 3
    assert sum(n for _, n, _ in ex.reqs) == 9  # no extra runs for the over-budget case


def test_behavioural_signature_ignores_the_median_rule(tmp_path, hist):
    r, ex, shas = mk(tmp_path, hist, lambda idx, c, n: 900)  # behavioural sig: token blow-up is a pass
    r.signature = FailureSignature(key="call_args:t.x", check="call_args", cases=["a", "b"])
    assert r.evaluate_commit(shas[0], "x").verdict == "good"


def test_signatures_at_uses_the_median_not_single_flags(tmp_path, hist):
    # discovery path (no signature): case a has one flagged repeat but a within-limit median; b is truly over
    vals = {"a": [100, 900, 100], "b": [200, 200, 100]}
    r, ex, shas = mk(tmp_path, hist, lambda idx, c, n: vals[c][n], sig=None)
    r.evaluate_commit(shas[0], "x", budget_cases=["a", "b"])
    sig = {d["key"]: d for d in r.signatures_at(shas[0], ["a", "b"])}
    assert sig["budget:tokens@x"]["cases"] == ["b"]


def test_steps_median_outlier_stays_good_and_baseline_cap_wins(tmp_path, hist):
    sig = FailureSignature(key="budget:steps@x", check="budget", cases=["a"])
    r, ex, shas = mk(tmp_path, hist, lambda *a: 100, sig=sig, cases=("a",))
    orig = ex.run_case

    def run(req):
        out = orig(req)
        for i, x in enumerate(out):
            x.steps, x.max_steps = [3, 9, 3][i], 4  # one outlier; the run-reported cap is 4
        return out
    ex.run_case = run
    res = r.evaluate_commit(shas[0], "x")
    assert res.verdict == "good" and res.budget["kind"] == "steps" and res.budget["cases"]["a"]["median"] == 3


def test_parse_report_without_budget_block_and_top_level_steps_win():
    rep = {"schema_version": 1, "results": [{"case_id": "a", "status": "pass"},
                                            {"case_id": "b", "status": "pass", "steps": 2, "budget": {"steps": 5}}]}
    a, b = parse_report(rep)
    assert (a.tokens, a.steps, a.max_steps) == (None, None, None) and b.steps == 2


def test_cache_detail_aggregate_and_old_records(tmp_path):
    c = StateStore(tmp_path).cache("/r")
    c.add_run("s", "a", PINS, 0, "pass", "t0", {}, tokens=10, steps=2, max_steps=6)
    c.add_run("s", "a", PINS, 1, "fail", "t1", {"budget:steps": {"key": "budget:steps"}})
    c._data[next(k for k in c._data if k.endswith("|r1"))].pop("tokens")  # a record written before tokens existed
    d = c.get("s", "a", PINS)["detail"]
    assert d[0] == {"status": "pass", "tokens": 10, "steps": 2, "max_steps": 6, "fails": []}
    assert d[1]["fails"] == ["budget:steps"] and d[1]["tokens"] is None
