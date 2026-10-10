import json
import subprocess

import pytest

from agentbisect.cli import main
from agentbisect.intake import (FixtureSource, IntakeError, NeatlogsCliSource, WaitingOnHumanLogin,
                                intake_detection, intake_issue, intake_trace, load_cases)
from agentbisect.models import RegressionSpec
from agentbisect.state import StateStore

from conftest import git

CASES = [
    {"id": "os1", "category": "order_status", "input": "Where is order 1042?", "checks": {"first_call": "lookup_order"}},
    {"id": "os2", "category": "order_status", "input": "Has order 1050 shipped yet?", "checks": {"must_call": ["lookup_order"]}},
    {"id": "os5", "category": "order_status", "input": "Did order 1060 get delivered?", "held_out": True, "checks": {}},
    {"id": "rw1", "category": "refund_within_policy", "input": "Order 1007 arrived broken, I want my money back",
     "checks": {"must_call": ["issue_refund"]}},
    {"id": "rw4", "category": "refund_within_policy", "input": "Refund my defective order 1020", "held_out": True, "checks": {}},
    {"id": "es3", "category": "escalation", "input": "I want a manager now", "checks": {}, "rubric": "polite"},
    {"id": "es1", "category": "escalation", "input": "Third time asking, escalate this", "checks": {}},
]


@pytest.fixture
def target(repo):
    """conftest repo (3 commits) plus evals/cases.jsonl in a 4th; returns (repo, [c1, c2, c3, c4])."""
    ev = repo.path / "evals"
    ev.mkdir()
    (ev / "cases.jsonl").write_text("\n".join(json.dumps(c) for c in CASES) + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c4 evals")
    return repo, repo.shas + [git(repo, "rev-parse", "HEAD")]


def tr(tid, commit, case, status="OK", started="2026-10-10T10:00:00Z", **kw):
    return {"traceId": tid, "workflowName": "support-agent", "status": status, "startedAt": started,
            "tags": [f"commit:{commit}", f"case:{case}", "run:r1"], **kw}


def fixtures(shas):
    c1, c2, c3, c4 = shas
    return {
        "traces": [
            tr("g1", c1[:10], "os1", started="2026-10-10T08:00:00Z"),
            tr("g2", c2, "os1", started="2026-10-10T09:00:00Z"),
            tr("gx", c2, "os2"),                                     # other case: ignored
            tr("later", c4, "os1", started="2026-10-10T12:00:00Z"),  # newer than the bad commit: ignored
            tr("err", c1, "os1", status="ERROR"),                    # errored trace is not 'passing'
            tr("bad", c3, "os1", started="2026-10-10T11:00:00Z"),
            {"traceId": "noTag", "workflowName": "support-agent", "status": "OK", "tags": [],
             "input": "Where is order 1042?", "startedAt": "2026-10-10T11:30:00Z"},
            {"traceId": "unmapped", "workflowName": "support-agent", "status": "OK", "tags": [], "input": "hi"},
        ],
        "detections": {"d1": {"displayName": "Wrong tool", "hits": ["unmapped", "bad"]},
                       "d0": {"displayName": "Empty", "hits": []}},
    }


def test_trace_intake(target):
    repo, (c1, c2, c3, c4) = target
    r = intake_trace(FixtureSource(fixtures((c1, c2, c3, c4))), "bad", repo)
    s = r.spec
    assert s.source == "trace" and s.bad_trace_id == "bad"
    assert s.good_trace_ids == ["g2", "g1"]  # newest earlier commit first; short sha tag resolved
    assert r.bad_sha == c3 and r.good_sha == c2
    assert s.cases == ["os1", "os2"]          # related category, working suite only
    assert s.held_out == ["os5", "rw4"]
    assert not set(s.cases) & set(s.held_out)
    assert [c["case_id"] for c in s.predicate["checks"]] == ["os1", "os2"]
    assert s.predicate["order"][0] == "checks" and s.predicate["budget"]["max_steps"] == 6
    assert s.predicate["rubric"] is None


def test_trace_case_found_by_input_when_untagged(target):
    repo, shas = target
    r = intake_trace(FixtureSource(fixtures(shas)), "noTag", repo)
    assert r.spec.cases == ["os1", "os2"] and any("no usable commit tag" in n for n in r.notes)
    assert r.bad_sha == shas[3]  # falls back to HEAD
    with pytest.raises(IntakeError, match="cannot map"):
        intake_trace(FixtureSource(fixtures(shas)), "unmapped", repo)


def test_held_out_isolation_when_bad_case_is_held_out(target):
    repo, shas = target
    d = fixtures(shas)
    d["traces"].append(tr("badho", shas[2], "os5"))
    r = intake_trace(FixtureSource(d), "badho", repo)
    assert "os5" not in r.spec.cases and "os5" in r.spec.held_out
    assert r.spec.cases == ["os1", "os2"]
    assert "os5" not in json.dumps(r.spec.predicate)  # held-out checks are not in the predicate either


def test_rubric_second_and_ground_truth_never_copied(target):
    repo, shas = target
    p = repo.path / "evals" / "cases.jsonl"
    p.write_text(p.read_text() + json.dumps({"id": "es9", "category": "escalation", "input": "x", "checks": {},
                                              "breaks": "R2", "intent_for": "R1"}) + "\n")
    d = fixtures(shas)
    d["traces"].append(tr("esbad", shas[2], "es1"))
    s = intake_trace(FixtureSource(d), "esbad", repo).spec
    assert s.cases == ["es3", "es1", "es9"]
    assert s.predicate["rubric"] == {"es3": "polite"}
    dumped = json.dumps(s.to_dict())
    assert "R2" not in dumped and "intent_for" not in dumped and "breaks" not in dumped
    assert not hasattr(load_cases(repo)[-1], "breaks")


def test_detection_intake_skips_unmappable_hits(target):
    repo, shas = target
    r = intake_detection(FixtureSource(fixtures(shas)), "d1", repo)
    assert r.spec.source == "detection" and r.spec.bad_trace_id == "bad"
    assert "Wrong tool" in r.notes[0]
    with pytest.raises(IntakeError, match="no hit traces"):
        intake_detection(FixtureSource(fixtures(shas)), "d0", repo)


ISSUE = {"number": 7, "title": "Agent tries to cancel when I ask where my order is",
         "body": "## Bug\n\nWhen I ask where order 1042 is, the support agent does something else.\n\nSteps..."}


def test_issue_intake_keywords(target):
    repo, _ = target
    r = intake_issue(ISSUE, repo)
    assert r.spec.source == "issue" and r.spec.bad_trace_id is None
    assert r.spec.cases == ["os1", "os2"] and r.spec.held_out == ["os5", "rw4"]
    with pytest.raises(IntakeError, match="could not map"):
        intake_issue({"title": "typo in footer", "body": ""}, repo)


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.models = reply, []

    def complete(self, system, prompt, model):
        self.models.append(model)
        return self.reply


def test_issue_intake_llm_uses_pinned_judge_and_validates(target, monkeypatch):
    repo, _ = target
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    llm = FakeLLM(json.dumps({"symptom": "refund wrong", "categories": ["refund_within_policy", "bogus"]}))
    r = intake_issue(ISSUE, repo, llm=llm)
    assert llm.models == ["claude-sonnet-5-5"]
    assert r.spec.cases == ["rw1"] and "rw4" in r.spec.held_out
    assert intake_issue(ISSUE, repo, llm=FakeLLM("not json")).spec.cases == ["os1", "os2"]  # keyword fallback
    monkeypatch.setenv("JUDGE_MODEL", "claude-opus-5")
    llm2 = FakeLLM("{}")
    intake_issue(ISSUE, repo, llm=llm2)
    assert llm2.models == ["claude-opus-5"]


def test_issue_fetch_uses_gh(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps(ISSUE), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    from agentbisect import intake
    assert intake.fetch_issue("https://github.com/o/r/issues/7")["number"] == 7
    assert seen["argv"][:3] == ["gh", "issue", "view"] and "--json" in seen["argv"]


# -- CLI source: auth failures and parsing of the real envelope shapes

def cli(responses):
    calls = []

    def run(argv):
        calls.append(argv)
        for key, resp in responses.items():
            if key in " ".join(argv):
                return resp
        return 2, json.dumps({"code": "INVALID_ARGUMENTS"}), ""
    return NeatlogsCliSource(project="p", run=run), calls


@pytest.mark.parametrize("resp", [
    (1, "", "Error: not logged in. Run neatlogs auth login"),
    (1, json.dumps({"code": "UNAUTHENTICATED", "detail": "x"}), ""),
    (1, "", json.dumps({"code": "TOKEN_EXPIRED"})),
    (1, "HTTP 401 Unauthorized", ""),
])
def test_auth_failure_is_waiting_on_human_login(resp):
    src, _ = cli({"traces": resp})
    with pytest.raises(WaitingOnHumanLogin, match="waiting on human login") as e:
        src.get_trace("t1")
    assert "auth login" in str(e.value) and "--device" in str(e.value)
    with pytest.raises(WaitingOnHumanLogin):
        src.list_traces()


def test_fixture_auth_failure_propagates_through_intake(target):
    repo, shas = target
    d = fixtures(shas)
    d["auth_failure"] = True
    with pytest.raises(WaitingOnHumanLogin):
        intake_trace(FixtureSource(d), "bad", repo)


def test_non_auth_cli_errors_are_not_login_errors():
    src, _ = cli({"traces": (2, json.dumps({"code": "MISSING_PROJECT_ID"}), "")})
    with pytest.raises(IntakeError, match="project id") as e:
        src.get_trace("t1")
    assert not isinstance(e.value, WaitingOnHumanLogin)
    with pytest.raises(IntakeError, match="not found"):
        NeatlogsCliSource(binary="/nonexistent/neatlogs").get_trace("t")


def test_cli_source_parses_real_envelopes():
    item = {"traceId": "T1", "workflowName": "support-agent", "status": "OK", "tags": ["commit:abc", "case:os1"],
            "startedAt": "2026-10-10T18:15:10.776Z"}
    ok = lambda data: (0, json.dumps({"data": data, "success": True}), "")  # noqa: E731
    src, calls = cli({
        "traces list": ok({"traces": [item], "page": {}}),
        "traces get": ok(item),
        "spans list": ok({"spans": [{"spanId": "S1", "parentSpanId": None}]}),
        "traces payload": ok({"fields": {"input": {"available": True, "value": "Where is order 1042?"}}}),
        "detections hits": ok({"hits": [{"traceId": "T9"}]}),
        "detections get": ok({"id": "d", "displayName": "X"}),
    })
    ts = src.list_traces("support-agent")
    assert ts[0].commit == "abc" and ts[0].case == "os1"
    argv = calls[0]
    assert "--workflow" in argv and "--credential-source" in argv and "--json" in argv
    assert argv[argv.index("--profile") + 1] == "neatlogs-us" and argv[argv.index("--host") + 1].startswith("https://app.")
    assert src.get_trace("T1").input == "Where is order 1042?"
    assert src.detection_hits("d") == ["T9"] and src.get_detection("d")["displayName"] == "X"


# -- spec round trip and CLI

def test_spec_json_roundtrip(target):
    repo, shas = target
    s = intake_trace(FixtureSource(fixtures(shas)), "bad", repo).spec
    assert RegressionSpec.from_dict(json.loads(json.dumps(s.to_dict()))) == s


def test_cli_investigate_writes_spec_to_state(target, tmp_path, capsys):
    repo, shas = target
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(fixtures(shas)))
    sd = tmp_path / "state"
    assert main(["--state-dir", str(sd), "investigate", "--trace", "bad", "--repo", str(repo),
                 "--fixtures", str(fx), "--intake-only"]) == 0
    st = StateStore(sd).list()[0]
    assert st.spec.bad_trace_id == "bad" and st.spec.cases == ["os1", "os2"] and st.bad_sha == shas[2]
    assert st.good_sha == shas[1] and st.status == "created"
    assert main(["--state-dir", str(sd), "intake", "--detection", "d1", "--repo", str(repo),
                 "--fixtures", str(fx)]) == 0
    assert '"source": "detection"' in capsys.readouterr().out
    assert len(StateStore(sd).list()) == 1  # intake writes nothing


def test_cli_auth_failure_exit_code(target, tmp_path, capsys):
    repo, shas = target
    d = fixtures(shas)
    d["auth_failure"] = True
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(d))
    assert main(["--state-dir", str(tmp_path / "s"), "investigate", "--trace", "bad", "--repo", str(repo),
                 "--fixtures", str(fx)]) == 3
    assert "waiting on human login" in capsys.readouterr().err
    assert StateStore(tmp_path / "s").list() == []
