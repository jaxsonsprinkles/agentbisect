"""Fix verification (chunk 11): target signature gone, no regressions, held-out cases, intent judge. Fakes only."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentbisect import cli
from agentbisect.baseline import Baseline
from agentbisect.models import CulpritReport, FailureSignature, FixAttempt, RegressionSpec
from agentbisect.runner import Runner, RunnerConfig, RunResult
from agentbisect.state import StateStore
from agentbisect.verify import (ClaudeIntentJudge, IntentVerdict, VerifierInput, describe_verification,
                                held_out_cases, parse_intent_reply, record_verification, verify_fix)
from conftest import git
from test_bisect import PINS, hist  # noqa: F401

SIG = FailureSignature(key="call_args:t.x", check="call_args", detail="t.x", cases=["a"])
BASE_IDX, FIX_IDX = 9, 10
WORK, HELD = ["a", "b", "c"], ["h1", "h2"]


def default(idx, case):
    """failing check keys of (commit index, case): the signature fails in case a at the bad end only."""
    return ["call_args:t.x"] if case == "a" and idx == BASE_IDX else []


class Exec:
    """behaviour(idx, case) -> failing keys; kb(idx, case, k) overrides it (k = repeat number); "ERROR" = infra error."""

    def __init__(self, behaviour=default, tokens=None, kb=None):
        self.behaviour, self.tokens, self.kb, self.n = behaviour, tokens, kb, {}

    def run_case(self, req):
        idx = int((req.workdir / "marker.txt").read_text())
        out = []
        for _ in range(req.repeats):
            k = self.n[(idx, req.case)] = self.n.get((idx, req.case), -1) + 1
            keys = self.kb(idx, req.case, k) if self.kb else self.behaviour(idx, req.case)
            if keys == "ERROR":
                out.append(RunResult(req.case, "error", reason="boom"))
                continue
            tok = self.tokens(idx, req.case, k) if self.tokens else None
            if tok is not None and tok > 150:
                keys = [*keys, "budget:tokens@x"]
            out.append(RunResult(req.case, "fail" if keys else "pass", trace_id=f"t{idx}{req.case}{k}",
                                 agent_model=PINS["agent_model"], judge_model=PINS["judge_model"], tokens=tok,
                                 failures=[{"key": x, "check": x.split(":")[0]} for x in keys]))
        return out


class Judge:
    def __init__(self, preserved=True, boom=False):
        self.preserved, self.boom, self.calls = preserved, boom, []

    def judge(self, intent, culprit_diff, fix_diff):
        self.calls.append((intent, culprit_diff, fix_diff))
        if self.boom:
            raise TimeoutError("judge down")
        return IntentVerdict(self.preserved, "keeps the refund feature" if self.preserved else "feature removed",
                             "claude-sonnet-5-5")


def mk(tmp_path, hist, ex=None, sig=SIG, held=HELD, intent="Add partial refunds", work=WORK, **cfg):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    runner = Runner(repo, RunnerConfig(cases=list(work), install_cmd=None, models=dict(PINS), backoff=0.0, workers=1, **cfg),
                    store.cache(str(repo)), ex or Exec(), sleep=lambda s: None, signature=sig)
    st = store.create(str(repo), good_sha=shas[0], bad_sha=shas[BASE_IDX],
                      spec=RegressionSpec(cases=list(work), held_out=list(held), signature=sig),
                      culprit=CulpritReport(sha=shas[5], checkpoint_intent=intent, intent_source="checkpoint"))
    return store, runner, st, VerifierInput(str(repo), shas[FIX_IDX], branch="fix/1")


def test_all_checks_pass(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    j = Judge()
    res = verify_fix(st, runner, vin, j)
    assert [c.name for c in res.checks] == ["target", "regression", "held_out", "intent"]
    assert res.passed is True and all(c.passed for c in res.checks)
    assert res.commit == hist[1][FIX_IDX] and res.eval_rounds > 0
    assert runner.signature is SIG  # restored for the caller
    intent, culprit_diff, fix_diff = j.calls[0]
    assert intent == "Add partial refunds" and "marker.txt" in fix_diff and "BROKEN" not in culprit_diff
    assert "VERIFIED" in describe_verification(res)[0]


def test_target_signature_still_reproduces(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: ["call_args:t.x"] if c == "a" and i >= BASE_IDX else []))
    res = verify_fix(st, runner, vin, Judge())
    t = res.check("target")
    assert t.passed is False and "still reproduces" in t.detail and res.passed is False


def test_other_regression_is_caught(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: default(i, c) or (["reply_regex:r"] if c == "b" and i == FIX_IDX else [])))
    res = verify_fix(st, runner, vin, Judge())
    reg = res.check("regression")
    assert res.check("target").passed is True and reg.passed is False
    assert reg.data["regressed"] == ["b"] and "b" in reg.detail and res.passed is False


def test_case_already_failing_before_is_not_a_regression(tmp_path, hist):
    # c fails at base and at the fix: pre-existing, not caused by the fix
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: default(i, c) or (["reply_regex:r"] if c == "c" and i >= BASE_IDX else [])))
    reg = verify_fix(st, runner, vin, Judge()).check("regression")
    assert reg.passed is True and "c" not in reg.data["cases_passing_before"]


def test_held_out_failure_means_overfit(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: default(i, c) or (["judge"] if c == "h2" and i == FIX_IDX else [])))
    res = verify_fix(st, runner, vin, Judge())
    h = res.check("held_out")
    assert h.passed is False and h.data["failing_cases"] == ["h2"] and res.passed is False
    assert res.check("target").passed and res.check("regression").passed


def test_no_held_out_cases_is_inconclusive(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, held=[])
    (hist[0] / "evals").mkdir()
    (hist[0] / "evals" / "cases.jsonl").write_text("")  # none flagged held_out either
    res = verify_fix(st, runner, vin, Judge())
    assert res.check("held_out").passed is None and res.passed is None
    assert "INCONCLUSIVE" in describe_verification(res)[0]


def commit_cases(repo, rows):
    (repo / "evals").mkdir(exist_ok=True)
    (repo / "evals" / "cases.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "cases")
    return git(repo, "rev-parse", "HEAD")


def test_held_out_read_from_fix_commit_and_unioned_with_recorded(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, held=["h9"])
    sha = commit_cases(hist[0], [{"id": "ro3", "category": "o", "input": "x", "held_out": True, "rubric": "r"},
                                 {"id": "a", "category": "o", "input": "x", "rubric": "r"}])
    assert held_out_cases(st, str(hist[0]), sha) == (["h9", "ro3"], ["ro3"])  # rubric ids only among held-out
    assert held_out_cases(st, str(hist[0]), vin.commit) == (["h9"], [])  # no file at that commit: recorded set only


def test_intent_not_preserved(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    res = verify_fix(st, runner, vin, Judge(preserved=False))
    i = res.check("intent")
    assert i.passed is False and "feature removed" in i.detail and res.passed is False


def test_intent_judge_outage_or_missing_is_inconclusive_not_pass(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    assert verify_fix(st, runner, vin, Judge(boom=True)).check("intent").passed is None
    assert verify_fix(st, runner, vin, None).passed is None
    store2, runner2, st2, vin2 = mk(tmp_path / "x", hist, intent="")
    assert verify_fix(st2, runner2, vin2, Judge()).check("intent").passed is None


def test_failure_in_one_check_does_not_hide_inconclusive_others(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: ["call_args:t.x"] if c == "a" and i >= BASE_IDX else []))
    res = verify_fix(st, runner, vin, Judge(boom=True))
    assert res.passed is False  # a fail beats an inconclusive


def test_budget_signature_uses_median_not_a_single_outlier(tmp_path, hist):
    bsig = FailureSignature(key="budget:tokens@x", check="budget", detail="tokens@x", cases=["a", "b"])
    base = Baseline({"token_factor": 1.5, "max_steps": 6,
                     "cases": {c: {"median_tokens": 100, "median_steps": 2, "n": 3} for c in "abc"}})

    def tokens(idx, case, k):  # base: all over budget; fix: one outlier run in three, median is fine
        return 400 if idx == BASE_IDX else (900 if k == 0 else 100)
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: [], tokens), sig=bsig)
    runner.baseline = base
    res = verify_fix(st, runner, vin, Judge())
    t = res.check("target")
    assert t.passed is True and t.data["budget"]["within"] == 2 and t.data["budget"]["repeats"] >= 3  # regression re-runs may add samples; the median still decides

    # median still over budget at the fix: not fixed
    store, runner, st, vin = mk(tmp_path / "y", hist, Exec(lambda i, c: [], lambda i, c, k: 400), sig=bsig)
    runner.baseline = base
    assert verify_fix(st, runner, vin, Judge()).check("target").passed is False


def test_unresolvable_commit_raises(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    with pytest.raises(RuntimeError):
        verify_fix(st, runner, VerifierInput(vin.repo, "deadbeef" * 5), Judge())


def test_record_verification_updates_attempt_and_state(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    st.status = "culprit_found"
    st.fix_attempts = [FixAttempt(branch="fix/1", attempt=1)]
    res = verify_fix(st, runner, vin, Judge())
    att = record_verification(store, st, vin, res)
    assert att is st.fix_attempts[0] and len(st.fix_attempts) == 1
    assert (att.eval_score, att.held_out_score, att.intent_test_passed) == (1.0, 1.0, True)
    assert st.status == "verifying" and st.metrics["verify_fix"][res.commit]["passed"] is True
    saved = store.load(st.id)
    assert saved.metrics["verify_fix"][res.commit]["checks"][0]["name"] == "target"
    # a branch the Fixer never recorded gets a new attempt; a state that cannot move to verifying is just saved
    st.status = "reported"
    record_verification(store, st, VerifierInput(vin.repo, vin.commit, branch="fix/2"), res)
    assert [a.branch for a in st.fix_attempts] == ["fix/1", "fix/2"] and st.status == "reported"


# ---------------------------------------------------------------- the judge itself

def test_parse_intent_reply():
    assert parse_intent_reply('noise {"preserved": true, "reason": "ok"} tail') == (True, "ok")
    assert parse_intent_reply('```json\n{"preserved": false, "reason": "reverted"}\n```') == (False, "reverted")
    for bad in ("", "nope", '{"preserved": "yes"}', "[1]"):
        with pytest.raises(ValueError):
            parse_intent_reply(bad)


def test_judge_refuses_haiku_tier():
    for m in ("claude-haiku-5-5", "", None):
        with pytest.raises(ValueError):
            ClaudeIntentJudge(m)


@pytest.fixture
def fake_api():
    seen = []
    reply = {"model": "claude-sonnet-5-5", "text": '{"preserved": true, "reason": "still works"}'}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((self.path, self.headers.get("x-api-key"), body))
            data = json.dumps({"model": reply["model"], "content": [{"type": "text", "text": reply["text"]}]}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, reply
    srv.shutdown()
    srv.server_close()


def test_claude_judge_pinned_model_and_request(fake_api):
    url, seen, reply = fake_api
    v = ClaudeIntentJudge("claude-sonnet-5-5", url=url).judge("intent text", "culprit diff", "fix diff")
    assert v == IntentVerdict(True, "still works", "claude-sonnet-5-5")
    path, key, body = seen[0]
    assert path == "/v1/messages" and key == "agentbisect-proxy"
    assert body["model"] == "claude-sonnet-5-5" and body["temperature"] == 0
    assert "intent text" in body["messages"][0]["content"] and "fix diff" in body["messages"][0]["content"]
    reply["model"] = "claude-haiku-5-5"  # a backend that answers with another model must not be trusted
    with pytest.raises(ValueError, match="pinned"):
        ClaudeIntentJudge("claude-sonnet-5-5", url=url).judge("i", "c", "f")


# ---------------------------------------------------------------- CLI

def _cli(monkeypatch, tmp_path, hist, capsys, judge, ex=None, commit=None):
    store, runner, st, vin = mk(tmp_path, hist, ex)
    monkeypatch.setattr("agentbisect.bisect.make_runner", lambda s, x: runner)
    monkeypatch.setattr("agentbisect.verify.ClaudeIntentJudge", lambda model: judge)
    code = cli.main(["--state-dir", str(tmp_path / "ab"), "verify-fix", st.id, "--commit", commit or vin.commit,
                     "--branch", "fix/1"])
    return code, capsys.readouterr(), store, st


def test_cli_verified_exit_0(monkeypatch, tmp_path, hist, capsys):
    code, out, store, st = _cli(monkeypatch, tmp_path, hist, capsys, Judge())
    assert code == 0 and "VERIFIED" in out.out and "[PASS] intent" in out.out
    assert store.load(st.id).fix_attempts[0].intent_test_passed is True


def test_cli_not_verified_exit_1(monkeypatch, tmp_path, hist, capsys):
    code, out, *_ = _cli(monkeypatch, tmp_path, hist, capsys, Judge(preserved=False))
    assert code == 1 and "NOT VERIFIED" in out.out and "[FAIL] intent" in out.out


def test_cli_inconclusive_exit_2(monkeypatch, tmp_path, hist, capsys):
    code, out, *_ = _cli(monkeypatch, tmp_path, hist, capsys, Judge(boom=True))
    assert code == 2 and "INCONCLUSIVE" in out.out


def test_cli_bad_commit_is_an_error_not_a_traceback(monkeypatch, tmp_path, hist, capsys):
    code, out, *_ = _cli(monkeypatch, tmp_path, hist, capsys, Judge(), commit="nosuchref")
    assert code == 1 and out.err.startswith("error:")


def test_cli_persists_result_on_failure_and_rejects_other_repo(monkeypatch, tmp_path, hist, capsys):
    code, out, store, st = _cli(monkeypatch, tmp_path, hist, capsys, Judge(preserved=False))
    assert code == 1
    assert store.load(st.id).metrics["verify_fix"][hist[1][FIX_IDX]]["passed"] is False
    code = cli.main(["--state-dir", str(tmp_path / "ab"), "verify-fix", st.id, "--commit", hist[1][FIX_IDX],
                     "--repo", str(tmp_path / "elsewhere")])
    assert code == 1 and "not the repo" in capsys.readouterr().err


# ---------------------------------------------------------------- review round: strictness, flakiness, errors

def test_one_failing_case_of_seven_cannot_hide_in_the_aggregate(tmp_path, hist):
    seven = [f"w{i}" for i in range(7)]
    sig = FailureSignature(key="call_args:t.x", check="call_args", detail="t.x", cases=seven)
    ex = Exec(lambda i, c: ["call_args:t.x"] if (i == BASE_IDX or (i == FIX_IDX and c == "w3")) else [])
    store, runner, st, vin = mk(tmp_path, hist, ex, sig=sig, work=seven)
    t = verify_fix(st, runner, vin, Judge()).check("target")
    assert t.data["verdict"] == "good" and t.data["pass_rate"] > 0.85  # 6/7 passes the band...
    assert t.passed is False and t.data["not_clean"] == ["w3"]  # ...but the case itself still fails


def test_one_failing_held_out_case_of_seven(tmp_path, hist):
    held = [f"h{i}" for i in range(7)]
    ex = Exec(lambda i, c: default(i, c) or (["judge"] if c == "h5" and i == FIX_IDX else []))
    store, runner, st, vin = mk(tmp_path, hist, ex, held=held)
    h = verify_fix(st, runner, vin, Judge()).check("held_out")
    assert h.data["verdict"] == "good" and h.passed is False and h.data["not_clean"] == ["h5"]


def test_held_out_rubric_cases_get_judge_repeats(tmp_path, hist):
    fix = commit_cases(hist[0], [{"id": "h1", "category": "o", "input": "x", "held_out": True, "rubric": "r"}])
    ex = Exec()
    store, runner, st, vin = mk(tmp_path, hist, ex, held=["h1", "h2"])
    verify_fix(st, runner, VerifierInput(vin.repo, fix), Judge())
    n = ex.n  # per (commit index, case) last repeat number
    assert n[(int((hist[0] / "marker.txt").read_text()), "h1")] + 1 == runner.cfg.judge_repeats
    assert runner.cfg.judge_cases == ()  # config restored


def test_skip_and_error_paths_are_inconclusive_not_crash_or_fail(tmp_path, hist):
    # every run at the fix errors: nothing executed -> skip verdicts -> pass_rate None
    ex = Exec(kb=lambda i, c, k: "ERROR" if i == FIX_IDX else default(i, c))
    store, runner, st, vin = mk(tmp_path, hist, ex)
    res = verify_fix(st, runner, vin, Judge())
    assert [c.passed for c in res.checks] == [None, None, None, True]
    assert res.passed is None and "could not be evaluated" in res.check("target").detail
    assert "INCONCLUSIVE" in describe_verification(res)[0]


def test_previously_passing_case_with_no_result_at_fix_is_regressed(tmp_path, hist):
    ex = Exec(kb=lambda i, c, k: "ERROR" if (i == FIX_IDX and c == "b") else default(i, c))
    store, runner, st, vin = mk(tmp_path, hist, ex)
    reg = verify_fix(st, runner, vin, Judge()).check("regression")
    assert reg.passed is False and reg.data["regressed"] == ["b"]


def test_one_noisy_failure_is_rerun_and_not_a_regression(tmp_path, hist):
    ex = Exec(kb=lambda i, c, k: default(i, c) or (["reply_regex:r"] if (i == FIX_IDX and c == "b" and k == 0) else []))
    store, runner, st, vin = mk(tmp_path, hist, ex)
    reg = verify_fix(st, runner, vin, Judge()).check("regression")
    assert reg.passed is True and "b" in reg.data["rerun_cases"]


def test_a_consistently_failing_case_is_a_regression_after_rerun(tmp_path, hist):
    ex = Exec(kb=lambda i, c, k: default(i, c) or (["reply_regex:r"] if (i == FIX_IDX and c == "b" and k < 3) else []))
    store, runner, st, vin = mk(tmp_path, hist, ex)
    reg = verify_fix(st, runner, vin, Judge()).check("regression")
    assert reg.passed is False and reg.data["regressed"] == ["b"]


def test_base_override_changes_what_counts_as_previously_passing(tmp_path, hist):
    beh = lambda i, c: default(i, c) or (["reply_regex:r"] if c == "c" and i >= 5 else [])  # noqa: E731
    store, runner, st, vin = mk(tmp_path, hist, Exec(beh))  # c already failed at the bad end (9)
    assert verify_fix(st, runner, vin, Judge()).check("regression").passed is True
    store, runner, st, vin = mk(tmp_path / "o", hist, Exec(beh))
    vin.base = hist[1][3]  # but c passed at commit 3
    reg = verify_fix(st, runner, vin, Judge()).check("regression")
    assert reg.passed is False and reg.data["regressed"] == ["c"]


def test_budget_signature_without_baseline_is_refused(tmp_path, hist):
    from agentbisect.baseline import BaselineError
    bsig = FailureSignature(key="budget:tokens@x", check="budget", detail="tokens@x", cases=["a"])
    store, runner, st, vin = mk(tmp_path, hist, sig=bsig)
    with pytest.raises(BaselineError):
        verify_fix(st, runner, vin, Judge())
    assert runner.signature is bsig


def test_signature_restored_when_a_check_raises(tmp_path, hist):
    class Boom:
        def run_case(self, req):
            raise RuntimeError("executor exploded")
    store, runner, st, vin = mk(tmp_path, hist, Boom())
    with pytest.raises(RuntimeError):
        verify_fix(st, runner, vin, Judge())
    assert runner.signature is SIG


def test_other_repo_is_refused(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    with pytest.raises(ValueError, match="not the repo"):
        verify_fix(st, runner, VerifierInput(str(tmp_path / "elsewhere"), vin.commit), Judge())


def test_attempt_matched_by_commit_when_recorded_without_branch_name(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist)
    st.fix_attempts = [FixAttempt(branch=vin.commit, attempt=1)]
    record_verification(store, st, vin, verify_fix(st, runner, vin, Judge()))
    assert len(st.fix_attempts) == 1 and st.fix_attempts[0].intent_test_passed is True


# ---- budget flags of OTHER regressions must not fail behaviourally fine cases

def _bloat(cases):
    """Cases whose only failure is a budget flag (token bloat from another, still-present regression)."""
    return lambda idx, case, k: 400 if case in cases else None


def test_other_regressions_budget_flags_are_ignored_by_regression_and_held_out(tmp_path, hist):
    store, runner, st, vin = mk(tmp_path, hist, Exec(default, _bloat({"b", "h1"})))
    res = verify_fix(st, runner, vin, Judge())
    reg, held = res.check("regression"), res.check("held_out")
    assert reg.passed is True and held.passed is True and res.passed is True
    assert reg.data["budget_ignored"] and "budget:* flags ignored" in reg.detail
    assert held.data["budget_ignored"] and "budget:* flags ignored" in held.detail
    assert runner.ignore_budget is False  # restored


def test_behavioural_failure_still_fails_when_budget_flags_are_ignored(tmp_path, hist):
    def beh(i, c):
        return default(i, c) or (["reply_regex:r"] if c == "h1" and i == FIX_IDX else [])
    store, runner, st, vin = mk(tmp_path, hist, Exec(beh, _bloat({"h1", "b"})))
    res = verify_fix(st, runner, vin, Judge())
    assert res.check("held_out").passed is False and res.check("regression").passed is True


def test_budget_flags_count_when_the_target_is_a_budget_signature(tmp_path, hist):
    bsig = FailureSignature(key="budget:tokens@x", check="budget", detail="tokens@x", cases=["a"])
    base = Baseline({"token_factor": 1.5, "max_steps": 6,
                     "cases": {c: {"median_tokens": 100, "median_steps": 2, "n": 3} for c in ("a", "b", "c", "h1", "h2")}})
    store, runner, st, vin = mk(tmp_path, hist, Exec(lambda i, c: [], _bloat({"h1"})), sig=bsig)
    runner.baseline = base
    res = verify_fix(st, runner, vin, Judge())
    held = res.check("held_out")
    assert held.passed is False and "h1" in held.detail and not held.data.get("budget_ignored")
    assert not res.check("regression").data.get("budget_ignored")
