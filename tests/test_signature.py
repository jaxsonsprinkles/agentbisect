import json

from agentbisect import bisect as B
from agentbisect import cli
from agentbisect import signature as S
from agentbisect.intake import FixtureSource, intake_trace
from agentbisect.models import FailureSignature, RegressionSpec
from agentbisect.runner import Runner, RunnerConfig, RunResult, parse_report
from agentbisect.state import StateStore
from test_bisect import PINS, hist  # noqa: F401  (hist is a fixture)

REASON = ("no issue_refund call matching {'order_id': '1007', 'amount': 64.0} "
          "(saw [{'order_id': '1007', 'amount': 6400}])")
AMOUNT = {"key": "call_args:issue_refund.amount", "check": "call_args", "detail": "issue_refund.amount",
          "expected": "64.0", "got": "6400", "reason": REASON}
BUDGET = {"key": "budget:steps", "check": "budget", "detail": "steps", "expected": "", "got": "",
          "reason": "7 steps > 6"}


def test_failure_records_from_harness_result():
    r = {"status": "fail", "checks": [{"check": "first_call", "passed": True, "reason": "ok"},
                                      {"check": "call_args", "passed": False, "reason": REASON}],
         "budget": {"status": "fail", "reason": "8 steps > 6"}, "judge": {"passed": False, "reason": "rude"}}
    recs = {x["key"]: x for x in S.failures_of_result(r)}
    assert set(recs) == {"call_args:issue_refund.amount", "budget:steps", "judge"}
    a = recs["call_args:issue_refund.amount"]
    assert (a["expected"], a["got"]) == ("64.0", "6400")  # order_id matched, only amount is the failure
    assert S.failures_of_result({"status": "pass", "checks": [], "budget": {"status": "pass"}}) == []
    assert S.failures_of_result({"status": "fail"})[0]["key"] == "case_fail"  # never silently a pass


def test_call_args_missing_call_and_numeric_tolerance():
    recs = S.failure_records("call_args", "no issue_refund call matching {'amount': 1.0} (saw [])")
    assert recs[0]["key"] == "call_args:issue_refund" and recs[0]["got"] == "no call"
    ok = S.failure_records("call_args", "no t call matching {'a': 1.0, 'b': 'x'} (saw [{'a': 1.004, 'b': 'y'}])")
    assert [r["key"] for r in ok] == ["call_args:t.b"]  # 1.004 is within half a cent


def test_parse_report_keeps_failing_checks_only_for_failed_runs():
    rep = {"schema_version": 1, "agent_model": "m", "judge_model": "j", "results": [
        {"case_id": "rw1", "status": "fail", "checks": [{"check": "call_args", "passed": False, "reason": REASON}]},
        {"case_id": "rw2", "status": "pass", "checks": []}]}
    f, p = parse_report(rep)
    assert f.failures[0]["key"] == "call_args:issue_refund.amount" and p.failures == []


def test_signature_round_trips_and_old_specs_load():
    sig = S.signature_from_record(AMOUNT)
    sig.cases = ["rw1"]
    spec = RegressionSpec(cases=["rw1"], signature=sig, bad_case="rw1")
    back = RegressionSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert back.signature == sig and back.bad_case == "rw1"
    old = {k: v for k, v in spec.to_dict().items() if k not in ("signature", "bad_case")}
    assert RegressionSpec.from_dict(old).signature is None
    assert "expected 64.0 got 6400" in sig.label


def test_signature_from_trace_hints():
    sig = S.signature_from_trace({"failed_checks": [{"check": "call_args", "reason": REASON}]}, [])
    assert sig.key == "call_args:issue_refund.amount" and sig.got == "6400"
    assert S.signature_from_trace({}, ["case:rw1", "check:budget:steps"]).key == "budget:steps"
    assert S.signature_from_trace({}, ["case:rw1"]) is None


def test_planner_feed_keywords():
    sig = S.signature_from_record(AMOUNT)
    assert S.symbol_hints(sig)[:2] == ["issue_refund", "_tool_issue_refund"] and "amount" in S.symbol_hints(sig)
    assert "unit conversion" in S.symptom_text(sig)  # 64.0 vs 6400


class OverlapExec:
    """Commit idx >= 3 adds R2 (budget failure in every case); idx >= 8 adds R3 (refund amount wrong in a and b)."""

    def __init__(self):
        self.runs = 0

    def run_case(self, req):
        idx = int((req.workdir / "marker.txt").read_text())
        out = []
        for i in range(req.repeats):
            self.runs += 1
            fails = []
            if idx >= 3:
                fails.append(BUDGET)
            if idx >= 8 and req.case in ("a", "b"):
                fails.append(AMOUNT)
            out.append(RunResult(req.case, "fail" if fails else "pass", trace_id=f"t{idx}-{req.case}-{i}",
                                 agent_model=PINS["agent_model"], judge_model=PINS["judge_model"], failures=fails))
        return out


def make(tmp_path, hist, spec=None, ex=None):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    runner = Runner(repo, RunnerConfig(cases=["a", "b", "c"], repeats=2, install_cmd=None, models=dict(PINS),
                                       backoff=0.0), store.cache(str(repo)), ex or OverlapExec(),
                    sleep=lambda s: None)
    st = store.create(str(repo), spec=spec or RegressionSpec(cases=["a", "b", "c"], bad_case="a"),
                      good_sha=shas[0], bad_sha=shas[-1])
    return store, runner, st, shas


def test_bisects_on_signature_not_case(tmp_path, hist):
    store, runner, st, shas = make(tmp_path, hist)
    out = B.run_investigation(store, st, runner)
    st = store.load(st.id)
    # the bad trace is in case a, whose amount failure starts at 8, although the case already fails from 3 (budget)
    assert out.kind == "culprit" and out.sha == shas[8]
    assert st.spec.signature.key == "call_args:issue_refund.amount" and st.spec.signature.cases == ["a", "b"]
    assert {d["key"] for d in st.metrics["signatures"]} == {"call_args:issue_refund.amount", "budget:steps"}
    mid = [r for sha, r in st.results.items() if shas.index(sha) in range(3, 8)]
    assert mid and all(r.verdict == "good" for r in mid)  # R2's budget failures count as a pass for this signature


def test_all_signatures_gives_one_culprit_each_and_shares_cache(tmp_path, hist):
    ex = OverlapExec()
    store, runner, st, shas = make(tmp_path, hist, ex=ex)
    B.run_investigation(store, st, runner)
    st = store.load(st.id)
    sibs = S.spawn_siblings(store, st)
    assert [s.spec.signature.key for s in sibs] == ["budget:steps"]
    assert S.spawn_siblings(store, store.load(st.id)) == []  # idempotent
    sib = store.load(sibs[0].id)
    r2 = Runner(st.repo, RunnerConfig(cases=["a", "b", "c"], repeats=2, install_cmd=None, models=dict(PINS)),
                store.cache(st.repo), ex, signature=sib.spec.signature, sleep=lambda s: None)
    out = B.run_investigation(store, sib, r2)
    assert out.sha == shas[3]
    assert store.load(st.id).metrics["signature_siblings"][0]["id"] == sib.id
    assert sib.metrics["signature_group"] == st.id


def test_resume_keeps_signature_without_rediscovery(tmp_path, hist):
    store, runner, st, shas = make(tmp_path, hist)
    B.run_investigation(store, st, runner)
    st = store.load(st.id)
    sig = st.spec.signature
    store.set_status(st, "running")
    n = runner.eval_rounds
    B.run_investigation(store, st, runner)
    assert store.load(st.id).spec.signature == sig and runner.eval_rounds == n  # all cached


def test_explicit_signature_skips_discovery(tmp_path, hist):
    spec = RegressionSpec(cases=["a", "b", "c"], signature=S.signature_from_key("budget:steps"))
    store, runner, st, shas = make(tmp_path, hist, spec=spec)
    runner.signature = spec.signature
    out = B.run_investigation(store, st, runner)
    st = store.load(st.id)
    assert out.sha == shas[3] and "signatures" not in st.metrics


def test_cache_records_per_check_failures(tmp_path, hist):
    store, runner, st, shas = make(tmp_path, hist)
    runner.evaluate_commit(shas[10], "x")
    e = runner.cache.get(shas[10], "a", runner.cfg.models)
    assert e["check_fails"] == {"budget:steps": 2, "call_args:issue_refund.amount": 2}
    assert runner.cache.get(shas[10], "c", runner.cfg.models)["check_fails"] == {"budget:steps": 2}


def test_revert_test_checks_the_signature_disappears_not_every_failure(tmp_path):
    """HEAD carries two regressions (R2 budget, R3 amount). Reverting R3 clears the amount signature even though
    the budget failure remains, so the revert test passes for this signature."""
    import subprocess
    from agentbisect import verify as V
    r = tmp_path / "t"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True)  # noqa: E731
    g("init", "-q", "-b", "main"), g("config", "user.email", "t@t"), g("config", "user.name", "t")
    shas = []
    for i, (name, body) in enumerate([("base", None), ("r2", "budget"), ("mid", None), ("r3", "amount"), ("tail", None)]):
        (r / "n.txt").write_text(f"{i}")
        if body:
            (r / f"{body}.flag").write_text("x")
        g("add", "-A"), g("commit", "-q", "-m", name)
        shas.append(g("rev-parse", "HEAD").stdout.strip())

    class FileExec:
        def run_case(self, req):
            fails = ([BUDGET] if (req.workdir / "budget.flag").exists() else []) + \
                    ([AMOUNT] if (req.workdir / "amount.flag").exists() else [])
            return [RunResult(req.case, "fail" if fails else "pass", trace_id=f"t{i}", failures=fails,
                              agent_model=PINS["agent_model"], judge_model=PINS["judge_model"])
                    for i in range(req.repeats)]

    store = StateStore(tmp_path / "ab")
    runner = Runner(r, RunnerConfig(cases=["a"], repeats=2, install_cmd=None, models=dict(PINS)),
                    store.cache(str(r)), FileExec(), sleep=lambda s: None)
    sig = S.signature_from_record(AMOUNT)
    sig.cases = ["a"]
    st = store.create(str(r), spec=RegressionSpec(cases=["a"], signature=sig), good_sha=shas[0], bad_sha=shas[-1])
    runner.signature = sig
    out = B.run_investigation(store, st, runner)
    assert out.sha == shas[3]
    st = store.load(st.id)
    res, _ = V.revert_test(st, runner)
    assert res.passed is True and res.details["signature"] == sig.key and res.details["reverted_pass_rate"] == 1.0
    # the same revert judged on the whole case would still fail (budget failure remains)
    whole = Runner(r, RunnerConfig(cases=["a"], repeats=2, install_cmd=None, models=dict(PINS)),
                   store.cache(str(r)), FileExec(), sleep=lambda s: None)
    assert whole.evaluate_commit(res.details["reverted_commit"], "w").verdict == "bad"


def test_intake_sets_bad_case_and_trace_signature(tmp_path):
    import subprocess
    r = tmp_path / "t"
    (r / "evals").mkdir(parents=True)
    (r / "evals" / "cases.jsonl").write_text(json.dumps(
        {"id": "rw1", "category": "refund", "input": "refund 1007", "checks": {}}) + "\n")
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
              ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(r), *c], check=True, capture_output=True)
    src = FixtureSource({"traces": [{"traceId": "T1", "status": "ok", "tags": ["case:rw1"],
                                     "metadata": {"failed_checks": [{"check": "call_args", "reason": REASON}]}}]})
    res = intake_trace(src, "T1", r)
    assert res.spec.bad_case == "rw1" and res.spec.signature.key == "call_args:issue_refund.amount"
    assert res.spec.signature.cases == ["rw1"]


def test_cli_flags_parse():
    a = cli._parser().parse_args(["investigate", "--trace", "t", "--repo", "r", "--signature", "budget:steps",
                                  "--no-all-signatures"])
    assert a.signature == "budget:steps" and a.all_signatures is False
    assert cli._parser().parse_args(["investigate", "--trace", "t", "--repo", "r"]).all_signatures is None



def test_budget_signature_is_qualified_by_category():
    r = {"status": "fail", "category": "order_status", "checks": [],
         "budget": {"status": "fail", "reason": "tokens 9000 > limit 5000"}}
    assert [x["key"] for x in S.failures_of_result(r)] == ["budget:tokens@order_status"]
    r.pop("category")
    assert [x["key"] for x in S.failures_of_result(r)] == ["budget:tokens"]  # old harness output unchanged


def test_all_categories_suite_spans_every_non_held_out_case(tmp_path):
    import subprocess
    r = tmp_path / "t"
    (r / "evals").mkdir(parents=True)
    rows = [{"id": "rw1", "category": "refund", "input": "refund 1007", "checks": {}},
            {"id": "os1", "category": "order", "input": "where 1042", "checks": {}},
            {"id": "os2", "category": "order", "input": "where 1043", "checks": {}, "held_out": True}]
    (r / "evals" / "cases.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
              ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(r), *c], check=True, capture_output=True)
    src = FixtureSource({"traces": [{"traceId": "T1", "status": "ok", "tags": ["case:rw1"]}]})
    assert intake_trace(src, "T1", r).spec.cases == ["rw1"]
    wide = intake_trace(src, "T1", r, all_categories=True).spec
    assert wide.cases == ["rw1", "os1"] and wide.held_out == ["os2"]
    assert cli._parser().parse_args(["investigate", "--trace", "t", "--repo", "r", "--all-categories"]).all_categories


class NoisyExec(OverlapExec):
    """Case c fails once, on its first run only, with a check nobody else fails (sampling noise)."""

    def run_case(self, req):
        out = super().run_case(req)
        if req.case == "c" and req.repeats and not getattr(self, "noised", False):
            self.noised = True
            out[0] = RunResult("c", "fail", trace_id="noise", agent_model=PINS["agent_model"],
                               judge_model=PINS["judge_model"],
                               failures=[{"key": "must_call", "check": "must_call", "detail": "", "expected": "",
                                          "got": "", "reason": "missing"}])
        return out


def test_single_run_noise_is_not_a_signature(tmp_path, hist):
    store, runner, st, shas = make(tmp_path, hist, ex=NoisyExec())
    runner.cfg.repeats = 1
    B.run_investigation(store, st, runner)
    st = store.load(st.id)
    assert "must_call" not in {d["key"] for d in st.metrics["signatures"]}
    assert [d["key"] for d in st.metrics["signatures_unconfirmed"]] == ["must_call"]
    assert {d["key"] for d in st.metrics["signatures"]} == {"call_args:issue_refund.amount", "budget:steps"}


def test_bare_budget_key_matches_category_qualified_failures(tmp_path, hist):
    qual = {**BUDGET, "key": "budget:steps@order"}

    class Q(OverlapExec):
        def run_case(self, req):
            out = super().run_case(req)
            for r in out:
                r.failures = [qual if f is BUDGET else f for f in r.failures]
            return out
    store, runner, st, shas = make(tmp_path, hist, ex=Q())
    st.spec.signature = runner.signature = FailureSignature(key="budget:steps")
    assert B.run_investigation(store, st, runner).sha == shas[3]


def test_signature_cases_absent_at_good_end_are_vacuously_good(tmp_path, hist):
    class Absent(OverlapExec):
        def run_case(self, req):
            idx = int((req.workdir / "marker.txt").read_text())
            if idx < 2:  # the feature behind the cases arrives at commit 2
                return [RunResult(req.case, "skipped", reason="requires tool 'x'") for _ in range(req.repeats)]
            return super().run_case(req)
    store, runner, st, shas = make(tmp_path, hist, ex=Absent())
    st.spec.signature = runner.signature = FailureSignature(key="budget:steps", cases=["a", "b", "c"])
    out = B.run_investigation(store, st, runner)
    assert out.kind == "culprit" and out.sha == shas[3]
    assert store.load(st.id).results[shas[0]].verdict == "good"
