import json
from pathlib import Path

import pytest

from agentbisect.culprit import trace_divergence
from agentbisect.fixtures import FIXTURE_TAG, FixtureDirSource, FixtureError, main, record_pair
from agentbisect.intake import FixtureSource, IntakeError, Span
from agentbisect.planner import Divergence, first_divergence

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "divergence" / "os1-search-help"


def report(commit, calls, trace_id, status="pass"):
    return {"schema_version": 1, "commit": commit, "run_id": "r", "started_at": "t", "agent_model": "a",
            "judge_model": "j", "results": [{"case_id": "c1", "repeat": 0, "status": status, "trace_id": trace_id,
                                             "steps": len(calls), "tokens": 10, "reply": "ok", "tool_calls": calls}]}


def call(name, **args):
    return {"name": name, "args": args, "result": {"ok": True}}


@pytest.fixture
def pair(tmp_path):
    good = report("g1", [call("lookup_order", order_id="1")], "tg")
    bad = report("b1", [call("lookup_order", order_id="1"), call("cancel_order", order_id="1")], "tb", "fail")
    return record_pair(good, bad, "c1", tmp_path / "fx", case_input="Where is it?")


def test_record_then_load_roundtrip(pair):
    src = FixtureDirSource(pair)
    assert (src.good_trace_id, src.bad_trace_id) == ("tg", "tb")
    t = src.get_trace("tb")
    assert t.input == "Where is it?" and t.commit == "b1" and t.case == "c1" and FIXTURE_TAG in t.tags
    assert [s.tools for s in src.spans("tb")] == [["lookup_order"], ["cancel_order"]]
    assert src.spans("tg")[0].args == {"order_id": "1"}


def test_divergence_extra_step_and_describe(pair):
    src = FixtureDirSource(pair)
    div = src.divergence()
    assert div.kind == "extra_step" and div.index == 1 and div.bad.tools == ["cancel_order"]
    text = src.describe()
    assert text.splitlines()[0].startswith("FIXTURE") and "not live neatlogs" in text.lower()
    assert "bad run made an extra step cancel_order" in text


def test_works_through_the_trace_source_interface(pair):
    class Spec:
        bad_trace_id, good_trace_ids = "tb", ["tg"]

    class St:  # minimal BisectState stand-in for what trace_divergence reads
        spec = Spec
        plan = None

    out = trace_divergence(St, FixtureDirSource(pair))
    assert out[0].startswith("first divergence:") and "cancel_order" in out[0]


def test_different_tool_args_and_identical(tmp_path):
    def src(name, g, b):
        return FixtureDirSource(record_pair(report("g", g, "tg"), report("b", b, "tb"), "c1", tmp_path / name))

    assert src("tool", [call("a", x=1)], [call("b", x=1)]).divergence().kind == "different_tool"
    assert src("args", [call("a", x=1)], [call("a", x=2)]).divergence().kind == "different_args"
    same = src("same", [call("a", x=1)], [call("a", x=1)])
    assert same.divergence() is None and "no divergence" in same.describe()


def test_missing_step_end_to_end(tmp_path):
    d = record_pair(report("g", [call("a"), call("b")], "tg"), report("b", [call("a")], "tb"), "c1", tmp_path / "fx")
    src = FixtureDirSource(d)
    assert src.divergence().kind == "missing_step" and "stopped before b" in src.describe()


def test_record_rejects_run_without_tool_calls(tmp_path):
    with pytest.raises(FixtureError):
        record_pair(report("g", [call("a")], "tg"), report("b", [], "tb"), "c1", tmp_path / "fx")


def test_long_results_truncated(tmp_path):
    big = {"name": "t", "args": {}, "result": "x" * 5000}
    d = record_pair(report("g", [big], "tg"), report("b", [big], "tb"), "c1", tmp_path / "fx")
    out = json.loads((d / "good.json").read_text())["spans"][0]["output"]
    assert len(out) < 400 and out.endswith("...")


def test_missing_case_raises(tmp_path):
    with pytest.raises(FixtureError):
        record_pair(report("g", [], "tg"), report("b", [], "tb"), "nope", tmp_path / "fx")


@pytest.mark.parametrize("breakage", ["no_manifest", "not_marked", "no_spans", "bad_json", "missing_file"])
def test_malformed_fixture_rejected(pair, breakage):
    m = pair / "manifest.json"
    if breakage == "no_manifest":
        m.unlink()
    elif breakage == "not_marked":
        m.write_text(json.dumps({**json.loads(m.read_text()), "fixture": False}))
    elif breakage == "no_spans":
        g = json.loads((pair / "good.json").read_text())
        g["spans"] = []
        (pair / "good.json").write_text(json.dumps(g))
    elif breakage == "bad_json":
        (pair / "bad.json").write_text("{nope")
    else:
        (pair / "bad.json").unlink()
    with pytest.raises(FixtureError):
        FixtureDirSource(pair)


def test_errors_are_intake_errors(pair):
    assert issubclass(FixtureError, IntakeError)
    with pytest.raises(IntakeError):
        FixtureDirSource(pair).get_trace("zzz")


def test_bundle_is_accepted_by_fixture_source(pair):
    plain = FixtureSource(FixtureDirSource(pair).bundle)
    assert first_divergence(plain.spans("tg"), plain.spans("tb")).kind == "extra_step"
    assert plain.get_trace("tg").workflow == "support-agent"


def test_cli_prints_divergence_and_writes_bundle(pair, tmp_path, capsys):
    out = tmp_path / "b.json"
    assert main([str(pair), "--bundle", str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("FIXTURE") and "first divergence" in printed and "wrote" in printed
    assert FIXTURE_TAG in json.loads(out.read_text())["traces"][0]["tags"]
    assert FixtureSource(out).spans("tb")
    assert main([str(tmp_path / "missing")]) == 2


def test_divergence_describe_handles_missing_side():
    assert "extra step" in Divergence(1, "extra_step", None, Span("s", tools=["t"])).describe()
    assert "stopped before" in Divergence(1, "missing_step", Span("s", tools=["t"]), None).describe()
    assert "stopped before llm_step" in Divergence(1, "missing_step", Span("llm_step"), None).describe()


def test_committed_real_fixture_loads_and_diverges():
    src = FixtureDirSource(REAL)
    assert src.manifest["fixture"] is True and src.label.startswith("FIXTURE")
    div = src.divergence()
    assert div.kind == "extra_step" and div.bad.tools == ["search_help"]
    assert len(src.spans(src.good_trace_id)) == 1 and len(src.spans(src.bad_trace_id)) == 4
