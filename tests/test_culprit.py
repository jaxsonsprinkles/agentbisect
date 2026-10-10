import json
import subprocess

import pytest

from agentbisect import bisect as B
from agentbisect import cli
from agentbisect import culprit as C
from agentbisect import verify as V
from agentbisect.intake import FixtureSource
from agentbisect.models import CommitResult, CulpritReport, RegressionSpec
from agentbisect.planner import _run
from agentbisect.runner import Runner, RunnerConfig, RunResult
from agentbisect.state import StateStore
from conftest import git
from test_bisect import PINS

CHECKPOINT = """● Checkpoint 01ABC
  session  s1
  commits  deadbee Add refunds
────────────────────────────────────────────────────────────
## Intent

You are a developer on this repo. Implement the following request, run the te...

## Summary

*Not generated yet. Run ‘entire checkpoint explain --generate 01ABC‘ to create an AI summary.*

## Files (1)

- `feature.txt`

── Transcript (full session) ───────────────────────────────
[User] You are a developer on this repo. Implement it.

Request: Make the agent refund directly when the customer mentions a refund.

[Tool] Bash: List files

[Assistant] I will call refund_order straight away to save a lookup step.

[Assistant] Done, tests pass.
"""
GRAPH_DIFF = {"files": [
    {"path": "tools/impl.py", "changes": [{"type": "body_changed", "kind": "function", "name": "ToolBox.refund",
                                            "dependents_count": 2}]},
    {"path": "tests/test_x.py", "changes": [{"type": "added", "kind": "function", "name": "test_refund"}]}]}


class Exec:
    """Fails whenever any tracked file contains REGRESSION (behaviour depends on the checked-out tree)."""

    def run_case(self, req):
        bad = any("REGRESSION" in p.read_text() for p in req.workdir.glob("*.txt"))
        return [RunResult(req.case, "fail" if bad else "pass", trace_id=f"t-{req.sha[:6]}-{i}",
                          agent_model=PINS["agent_model"], judge_model=PINS["judge_model"])
                for i in range(req.repeats)]


def make_run(checkpoint=CHECKPOINT, graph=None):
    """Fake `entire` binary; everything else (git) runs for real."""
    def run(argv, cwd):
        if argv[0] == "entire":
            if argv[1:3] == ["checkpoint", "explain"]:
                return (0, checkpoint if checkpoint is not None else "Commit has no Entire-Checkpoint trailer", "")
            if argv[1:3] == ["graph", "diff"]:
                return (0, json.dumps(graph), "") if graph else (127, "", "entire: not found")
            return 1, "", "unsupported"
        return _run(argv, cwd)
    return run


def commit(r, msg, **files):
    for name, text in files.items():
        (r / name.replace("_", ".")).write_text(text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", msg)
    return git(r, "rev-parse", "HEAD")


def new_repo(tmp_path):
    r = tmp_path / "target"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    return r


@pytest.fixture
def hist(tmp_path):
    """c0..c7; c3 (shas[3]) writes REGRESSION into a.txt; all others touch only other.txt."""
    r = new_repo(tmp_path)
    shas = [commit(r, "c0: base", a_txt="ok\n", other_txt="0\n")]
    for i in range(1, 8):
        if i == 3:
            shas.append(commit(r, "c3: auto refund on mention", a_txt="ok\nREGRESSION\n"))
        else:
            shas.append(commit(r, f"c{i}: housekeeping", other_txt=f"{i}\n"))
    return r, shas


def mk_state(tmp_path, r, shas, culprit_idx, *, confidence="high", cases=("a", "b")):
    store = StateStore(tmp_path / "ab")
    runner = Runner(r, RunnerConfig(cases=list(cases), repeats=2, install_cmd=None, models=dict(PINS), backoff=0.0),
                    store.cache(str(r)), Exec(), sleep=lambda s: None)
    st = store.create(str(r), spec=RegressionSpec(cases=list(cases), bad_trace_id="bad-1", good_trace_ids=["good-1"]),
                      good_sha=shas[0], bad_sha=shas[-1])
    st.results[shas[0]] = CommitResult("good", 1.0, executed=4)
    st.results[shas[culprit_idx]] = CommitResult("bad", 0.0, executed=4, failing_cases=["a"], trace_ids=["tr-bad"])
    st.culprit = CulpritReport(sha=shas[culprit_idx], confidence=confidence)
    st.status = "running"
    store.set_status(st, "culprit_found")
    return store, runner, st


# ------------------------------------------------------------------ revert test

def test_normal_revert_passes_end_to_end(tmp_path, hist):
    r, shas = hist
    store = StateStore(tmp_path / "ab")
    runner = Runner(r, RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS), backoff=0.0),
                    store.cache(str(r)), Exec(), sleep=lambda s: None)
    st = store.create(str(r), spec=RegressionSpec(cases=["a", "b"]), good_sha=shas[0], bad_sha=shas[-1])
    assert B.run_investigation(store, st, runner).sha == shas[3]
    rounds_before = st.metrics["eval_rounds"]
    C_run = make_run(graph=GRAPH_DIFF)
    rep = V.confirm_culprit(store, st, runner, run=C_run)
    assert rep.revert_test_passed is True and rep.confidence == "high" and not rep.flags
    assert rep.intent_source == "checkpoint" and "refund directly" in rep.checkpoint_intent
    assert "refund_order straight away" in rep.checkpoint_intent
    assert st.status == "culprit_found"
    rt = st.metrics["revert_test"]
    assert rt["passed"] is True and rt["eval_rounds"] == 4 and rt["reverted_pass_rate"] == 1.0
    assert st.metrics["eval_rounds"] == rounds_before + 4
    # persisted + visible in status
    st2 = store.load(st.id)
    assert st2.culprit.revert_test_passed is True and st2.culprit.analyzed
    text = "\n".join(V.describe_revert(st2))
    assert "PASSED" in text and shas[3] in text
    # idempotent: a second call runs nothing
    before = runner.eval_rounds
    V.confirm_culprit(store, st2, runner, run=C_run)
    assert runner.eval_rounds == before
    # the repo is left clean (no stray worktrees)
    assert "worktree" not in git(r, "worktree", "list").replace(str(r), "").replace("[main]", "")


def test_revert_does_not_fix_wrong_culprit(tmp_path, hist):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 7)  # c7 only touches other.txt; REGRESSION from c3 stays
    rep = V.confirm_culprit(store, st, runner, run=make_run())
    assert rep.revert_test_passed is False and rep.confidence == "low"
    assert V.MULTI_REGRESSION_FLAG in rep.flags
    assert V.MULTI_REGRESSION_FLAG in st.metrics["warnings"]
    assert "FAILED" in "\n".join(V.describe_revert(st))


def test_revert_conflict_nothing_applicable_is_inconclusive(tmp_path):
    r = new_repo(tmp_path)
    shas = [commit(r, "base", a_txt="mode=ok\n")]
    shas.append(commit(r, "culprit", a_txt="mode=REGRESSION\n"))
    shas.append(commit(r, "later edit same line", a_txt="mode=REGRESSION2\n"))
    store, runner, st = mk_state(tmp_path, r, shas, 1)
    rep = V.confirm_culprit(store, st, runner, run=make_run())
    assert rep.revert_test_passed is None and rep.confidence == "medium"  # high downgraded one notch
    assert V.REVERT_CONFLICTS in rep.flags and rep.revert_details["kind"] == "conflict"
    assert "inconclusive" in "\n".join(V.describe_revert(st))
    assert git(r, "status", "--porcelain") == ""


def test_revert_conflict_partial_revert_still_proves_culprit(tmp_path):
    r = new_repo(tmp_path)
    shas = [commit(r, "base", a_txt="ok\n", b_txt="x=1\n")]
    shas.append(commit(r, "culprit", a_txt="ok\nREGRESSION\n", b_txt="x=2\n"))
    shas.append(commit(r, "later edit of b", b_txt="x=3\n"))  # b.txt hunk no longer reverts cleanly
    store, runner, st = mk_state(tmp_path, r, shas, 1)
    rep = V.confirm_culprit(store, st, runner, run=make_run())
    assert rep.revert_test_passed is True
    assert V.REVERT_CONFLICTS in rep.flags and rep.revert_details["kind"] == "partial"
    assert rep.revert_details["rejected_hunks"] >= 1
    assert rep.confidence == "medium"


def test_revert_failing_to_build_is_inconclusive(tmp_path, hist):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 3)
    runner.cfg.install_cmd = ["false"]
    rep = V.confirm_culprit(store, st, runner, run=make_run())
    assert rep.revert_test_passed is None and rep.confidence == "medium"


def test_revert_throwaway_commit_is_deterministic(tmp_path, hist):
    r, shas = hist
    a = V.build_reverted_commit(r, shas[3], shas[-1])
    b = V.build_reverted_commit(r, shas[3], shas[-1])
    assert a.sha and a.sha == b.sha and a.kind == "clean"


# ------------------------------------------------------------------ analyst

def test_parse_checkpoint_prompt_and_reasoning():
    prompt, reasoning = C.parse_intent_parts(CHECKPOINT)
    assert prompt == "Make the agent refund directly when the customer mentions a refund."
    assert "straight away" in reasoning and "Tool" not in reasoning
    assert C.parse_intent_parts("Commit abc has no Entire-Checkpoint trailer") is None


def test_no_checkpoint_falls_back_to_commit_message_and_git_diff(tmp_path, hist):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 3)
    rep = C.analyze_culprit(st, shas[3], run=make_run(checkpoint=None))
    assert rep.intent_source == "commit_message" and "auto refund on mention" in rep.checkpoint_intent
    assert any("no checkpoint" in f for f in rep.flags)
    assert "a.txt" in rep.semantic_diff  # git diff --stat fallback
    assert "a.txt" in rep.impacted_symbols
    assert any("graph unavailable" in n for n in rep.revert_details["analysis_notes"])


def test_semantic_diff_from_entire_graph(tmp_path, hist):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 3)
    rep = C.analyze_culprit(st, shas[3], run=make_run(graph=GRAPH_DIFF))
    assert "tools/impl.py: body_changed function ToolBox.refund (2 dependents)" in rep.semantic_diff
    assert "ToolBox.refund" in rep.impacted_symbols and "test_refund" not in rep.impacted_symbols


def test_trace_divergence_span_by_span(tmp_path, hist):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 3)
    span = lambda t: {"name": "step", "kind": "tool", "tools": [t]}
    src = FixtureSource_from({"good-1": [span("lookup_order"), span("reply")], "bad-1": [span("refund_order")]})
    lines = C.trace_divergence(st, src)
    assert "bad run chose refund_order instead of lookup_order" in lines[0]
    assert any("step 0" in ln and "differs" in ln for ln in lines)
    assert any("step 1" in ln and "(none)" in ln for ln in lines)
    assert "unavailable" in C.trace_divergence(st, None)[0]


def FixtureSource_from(spans):
    import tempfile, os
    p = os.path.join(tempfile.mkdtemp(), "f.json")
    json.dump({"spans": spans}, open(p, "w"))
    return FixtureSource(p)


def test_fixer_brief_has_only_culprit_diff_intent_and_evidence(tmp_path, hist):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 3)
    rep = C.analyze_culprit(st, shas[3], run=make_run(graph=GRAPH_DIFF))
    brief = C.fixer_brief(str(r), rep)
    assert "+REGRESSION" in brief and C.INTENT_LABEL in brief and "refund directly" in brief
    assert "failing cases: a" in brief and "failing trace: bad-1" in brief
    for i, s in enumerate(shas):
        assert s not in brief and s[:7] not in brief
        if i != 3:
            assert f"c{i}:" not in brief
    assert "housekeeping" not in brief and "base" not in brief.split("## Failing")[0].split("```diff")[0]
    assert git(r, "log", "-1", "--format=%s", shas[3]) not in brief  # not even the commit message: it is in the intent


# ------------------------------------------------------------------ cli

def test_status_shows_culprit_and_revert_result(tmp_path, hist, capsys):
    r, shas = hist
    store, runner, st = mk_state(tmp_path, r, shas, 3)
    V.confirm_culprit(store, st, runner, run=make_run())
    assert cli.main(["--state-dir", str(store.base), "status", st.id]) == 0
    out = capsys.readouterr().out
    assert f"culprit: {shas[3]}" in out and "revert test: PASSED" in out and "intent source: checkpoint" in out
