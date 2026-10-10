import json
import os

import pytest

from agentbisect import state as state_mod
from agentbisect.cli import main
from agentbisect.models import (BisectState, Candidate, CommitResult, CulpritReport, FixAttempt, Plan,
                                RegressionSpec)
from agentbisect.state import StateError, StateStore, atomic_write_json


def full_state():
    return BisectState(
        id="x", repo="/r", good_sha="g", bad_sha="b",
        spec=RegressionSpec("trace", "bt", ["g1"], ["os1"], ["os5"], {"checks": ["a"], "rubric": None}),
        plan=Plan("span", ["sym"], [Candidate("abc", 0.9, "touches tool")], "smart"),
        results={"abc": CommitResult("bad", 0.2, ["t1"], executed=3, case_stats={"os1": {"passes": 0, "fails": 3, "errors": 0}})},
        step=2, status="running", context_tokens_per_step=[{"step": 1, "label": "graph", "tokens": 5}],
        pinned_models={"agent_model": "h"}, culprit=CulpritReport("abc", "why", "diff", ["s"], True, "high"),
        fix_attempts=[FixAttempt("fix/1", "d", 0.9, 0.8, True, ["t"], 1)])


def test_models_json_roundtrip():
    st = full_state()
    d = json.loads(json.dumps(st.to_dict()))
    assert BisectState.from_dict(d) == st
    assert BisectState.from_dict(BisectState(id="e").to_dict()) == BisectState(id="e")


def test_atomic_write_leaves_old_file_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    atomic_write_json(p, {"v": 1})

    def boom(a, b):
        raise OSError("disk")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"v": 2})
    monkeypatch.undo()
    assert json.loads(p.read_text()) == {"v": 1}
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]  # tmp cleaned up


def test_atomic_write_never_exposes_partial_file(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    atomic_write_json(p, {"v": 1})
    seen = []
    real = os.replace

    def spy(a, b):
        seen.append(json.loads(p.read_text()))  # at replace time target is still the complete old file
        real(a, b)
    monkeypatch.setattr(os, "replace", spy)
    atomic_write_json(p, {"v": 2})
    assert seen == [{"v": 1}] and json.loads(p.read_text()) == {"v": 2}


def test_status_transitions(tmp_path):
    s = StateStore(tmp_path)
    st = s.create(str(tmp_path))
    with pytest.raises(StateError):
        s.set_status(st, "reported")
    s.set_status(st, "running")
    s.set_status(st, "paused")
    s.set_status(st, "running")
    s.set_status(st, "culprit_found")
    s.set_status(st, "reported")
    with pytest.raises(StateError):
        s.resume(st)
    with pytest.raises(StateError):
        s.set_status(st, "bogus")


def test_step_persisted_every_time_and_context_tokens(tmp_path):
    s = StateStore(tmp_path)
    st = s.create(str(tmp_path))
    s.set_status(st, "running")
    s.begin_step(st, "test", sha="abc")
    assert StateStore(tmp_path).load(st.id).in_flight["sha"] == "abc"
    s.log_context_tokens(st, "with_graph", 1234)
    s.end_step(st, "abc", CommitResult("good", 1.0))
    again = StateStore(tmp_path).load(st.id)
    assert again.step == 1 and again.in_flight is None and again.results["abc"].verdict == "good"
    assert again.context_tokens_per_step == [{"step": 1, "label": "with_graph", "tokens": 1234}]


def test_cli_status_and_resume(tmp_path, capsys):
    s = StateStore(tmp_path)
    st = s.create(str(tmp_path), bad_sha="b" * 40)
    s.set_status(st, "running")
    s.begin_step(st, "test", sha="abc")
    assert main(["--state-dir", str(tmp_path), "status"]) == 0
    assert "INTERRUPTED" in capsys.readouterr().out
    assert main(["--state-dir", str(tmp_path), "resume", st.id]) == 1  # engine ran; bare state has no eval cases
    assert "redo interrupted step" in capsys.readouterr().out
    assert main(["--state-dir", str(tmp_path), "resume", "nope"]) == 1
    assert main(["--state-dir", str(tmp_path), "watch"]) == 2
