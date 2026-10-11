"""Tool-count signal: overlapping token regressions are separated by which tool's call count rose."""
import threading

import pytest

from agentbisect import bisect as B
from agentbisect.baseline import Baseline, BaselineError, build_baseline, tool_of
from agentbisect.models import FailureSignature, RegressionSpec
from agentbisect.runner import Runner, RunnerConfig, RunResult, parse_report
from agentbisect.signature import spawn_siblings
from agentbisect.state import StateStore
from test_bisect import PINS, hist  # noqa: F401

V2 = Baseline({"baseline_version": 2, "token_factor": 1.5, "max_steps": 6, "cases": {
    c: {"median_tokens": 100, "median_steps": 2, "n": 3, "tool_counts": {"lookup_order": 1}} for c in "ab"}})
V1 = Baseline({"token_factor": 1.5, "max_steps": 6, "cases": {c: {"median_tokens": 100} for c in "ab"}})


class Exec:
    """R2 (search_help +1 call, +60 tokens) from commit 3, R1 (cancel_order +2 calls, +60 tokens) from commit 6;
    both inflate tokens in every case. A prompt-size bump (tokens only) from commit 9 changes no call count."""

    def __init__(self, noisy_repeat=None, flat_calls=False):
        self.lock, self.seen, self.noisy, self.flat = threading.Lock(), {}, noisy_repeat, flat_calls

    def run_case(self, req):
        idx = int((req.workdir / "marker.txt").read_text())
        out = []
        with self.lock:
            for _ in range(req.repeats):
                n = self.seen[(idx, req.case)] = self.seen.get((idx, req.case), -1) + 1
                counts = {"lookup_order": 1}
                tok = 100 + 60 * (idx >= 3) + 60 * (idx >= 6) + 80 * (idx >= 9)
                if idx >= 3 and not self.flat:
                    counts["search_help"] = 1
                if idx >= 6 and not self.flat:
                    counts["cancel_order"] = 2
                if self.noisy == (idx, n):  # one outlier repeat: the median must absorb it
                    counts["cancel_order"] = 5
                over = tok > 150
                out.append(RunResult(req.case, "fail" if over else "pass", trace_id=f"t{idx}{n}",
                                     agent_model=PINS["agent_model"], judge_model=PINS["judge_model"],
                                     failures=[{"key": "budget:tokens@x", "check": "budget"}] if over else [],
                                     tokens=tok, steps=2, max_steps=6, tool_counts=counts))
        return out


def mk(tmp_path, hist, baseline=V2, sig=None, **kw):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    r = Runner(repo, RunnerConfig(cases=["a", "b"], install_cmd=None, models=dict(PINS), workers=1, backoff=0.0),
               store.cache(str(repo)), Exec(**kw), sleep=lambda s: None, signature=sig)
    r.baseline = baseline
    return store, r, shas


def test_parse_report_counts_tool_calls_per_run():
    rep = {"schema_version": 1, "agent_model": "m", "judge_model": "j", "results": [
        {"case_id": "a", "status": "pass", "tool_calls": [{"name": "lookup_order"}, {"name": "search_help"},
                                                           {"name": "search_help"}]},
        {"case_id": "b", "status": "pass"}]}
    a, b = parse_report(rep)
    assert a.tool_counts == {"lookup_order": 1, "search_help": 2} and b.tool_counts is None


def test_tool_of():
    assert tool_of("budget:tokens@order_status+tool:cancel_order") == "cancel_order"
    assert tool_of("budget:tokens@order_status") is None and tool_of(None) is None


def test_build_baseline_records_median_tool_counts_and_bumps_version():
    e = {"detail": [{"status": "pass", "tokens": 100, "steps": 2, "fails": [], "tool_counts": tc}
                    for tc in ({"lookup_order": 1}, {"lookup_order": 1, "search_help": 1}, {"lookup_order": 2})]}
    out = build_baseline({"a": e}, "a" * 40, PINS, 3)
    assert out["cases"]["a"]["tool_counts"] == {"lookup_order": 1, "search_help": 0}
    assert out["baseline_version"] == 2
    b = Baseline(out)
    assert b.has_tool_counts and b.tool_median("a", "search_help") == 0 and b.tool_median("a", "never") == 0
    assert b.tool_median("zz", "x") is None
    # no counts recorded (older harness): no key, no version bump, nothing for the tool signal to use
    old = build_baseline({"a": {"detail": [{"status": "pass", "tokens": 5, "steps": 1, "fails": []}]}}, "a" * 40, PINS, 3)
    assert "tool_counts" not in old["cases"]["a"] and "baseline_version" not in old
    assert not Baseline(old).has_tool_counts and not V1.has_tool_counts


def test_old_baseline_rules_unchanged_and_tool_signature_needs_version_2(tmp_path, hist):
    sig = FailureSignature(key="budget:tokens@x+tool:cancel_order", check="budget", cases=["a", "b"])
    _, r, shas = mk(tmp_path, hist, baseline=V1, sig=sig)
    with pytest.raises(BaselineError, match="calibrate"):
        r.evaluate_commit(shas[0], "x")
    with pytest.raises(BaselineError, match="no good_sha"):  # the v1 rejection rules still apply to any baseline
        V2.validate("a" * 40, PINS["agent_model"])


def test_predicate_is_median_call_count_over_baseline(tmp_path, hist):
    sig = FailureSignature(key="budget:tokens@x+tool:cancel_order", check="budget", cases=["a", "b"])
    _, r, shas = mk(tmp_path, hist, sig=sig, noisy_repeat=(2, 0))  # a lone 5-call repeat at commit 2: median says fine
    assert [r.evaluate_commit(shas[i], f"x{i}").verdict for i in (0, 2, 5, 6, 13)] == \
        ["good", "good", "good", "bad", "bad"]  # tokens are already over from commit 3, calls only from 6
    res = r.evaluate_commit(shas[13], "y")
    assert res.budget["kind"] == "cancel_order calls" and res.budget["median"] == 2 and res.budget["within"] == 0
    assert res.repeats == 3 and r.eval_rounds == 5 * 6  # the existing 3 repeats per case, nothing extra


def test_refine_splits_overlapping_regressions_and_keeps_prompt_only_budgets(tmp_path, hist):
    _, r, shas = mk(tmp_path, hist)
    tok = FailureSignature(key="budget:tokens@x", check="budget", cases=["a", "b"], reason="r")
    r.signature = tok
    r.evaluate_commit(shas[13], "x")
    refined = r.refine_budget_signatures(shas[13], [tok])
    assert [s.key for s in refined] == ["budget:tokens@x+tool:cancel_order", "budget:tokens@x+tool:search_help"]
    assert refined[0].cases == ["a", "b"] and "rose by 2" in refined[0].reason
    r.evaluate_commit(shas[4], "z")  # tokens over, but only search_help has risen by commit 4
    assert [s.key for s in r.refine_budget_signatures(shas[4], [tok])] == ["budget:tokens@x+tool:search_help"]
    r.baseline = V1  # no tool counts in the baseline: unchanged
    assert r.refine_budget_signatures(shas[13], [tok]) == [tok]


def test_token_budget_without_any_rising_tool_stays_a_plain_token_signature(tmp_path, hist):
    _, r, shas = mk(tmp_path, hist, flat_calls=True)
    tok = FailureSignature(key="budget:tokens@x", check="budget", cases=["a", "b"])
    r.signature = tok
    assert r.evaluate_commit(shas[13], "x").verdict == "bad"
    assert r.refine_budget_signatures(shas[13], [tok]) == [tok]


def test_investigation_maps_each_overlapping_regression_to_its_own_commit(tmp_path, hist):
    store, r, shas = mk(tmp_path, hist)
    st = store.create(str(hist[0]), spec=RegressionSpec(cases=["a", "b"], bad_case="a"), good_sha=shas[0],
                      bad_sha=shas[-1])
    st.metrics["only_budget"] = True
    out = B.run_investigation(store, st, r)
    st = store.load(st.id)
    keys = {d["key"] for d in st.metrics["signatures"]}
    assert keys == {"budget:tokens@x+tool:cancel_order", "budget:tokens@x+tool:search_help"}
    assert st.spec.signature.key == "budget:tokens@x+tool:cancel_order" and out.sha == shas[6]
    (sib,) = spawn_siblings(store, st)
    sib_out = B.run_investigation(store, sib, _runner_for(r, sib))
    assert sib.spec.signature.key.endswith("+tool:search_help") and sib_out.sha == shas[3]


def _runner_for(r, sib):
    r2 = Runner(r.repo, r.cfg, r.cache, r.executor, sleep=lambda s: None, signature=sib.spec.signature)
    r2.baseline = r.baseline
    return r2


def test_v1_baseline_warns_instead_of_refining(tmp_path, hist):
    store, r, shas = mk(tmp_path, hist, baseline=V1)
    st = store.create(str(hist[0]), spec=RegressionSpec(cases=["a", "b"], bad_case="a"), good_sha=shas[0],
                      bad_sha=shas[-1])
    B.run_investigation(store, store.load(st.id), r)
    st = store.load(st.id)
    assert st.spec.signature.key == "budget:tokens@x"
    assert any("version 1" in w for w in st.metrics["warnings"])
