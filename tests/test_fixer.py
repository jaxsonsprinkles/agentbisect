import json
import subprocess
from types import SimpleNamespace

import pytest

from agentbisect import cli
from agentbisect.culprit import fixer_brief
from agentbisect.fixer import ProxyFixModel, RunnerChecker, apply_edits, build_prompt, parse_reply, run_fixer
from agentbisect.models import BisectState, CommitResult, CulpritReport, FixAttempt, FixResult
from agentbisect.state import StateStore
from conftest import git


def reply(*files, expl="fixed"):
    return json.dumps({"explanation": expl, "files": list(files)})


class FakeModel:
    def __init__(self, *replies):
        self.replies, self.prompts, self.systems = list(replies), [], []

    def complete(self, system, prompt):
        self.systems.append(system)
        self.prompts.append(prompt)
        return self.replies.pop(0)


class FakeChecker:
    """Passes when marker.txt says 'fixed'; records what it was asked."""

    def __init__(self, repo):
        self.repo, self.calls = repo, []

    def check(self, commit, attempt):
        self.calls.append((commit, attempt))
        content = git(self.repo, "show", f"{commit}:marker.txt")
        return (True, []) if content == "fixed" else (False, [f"failing check: marker is {content!r}"])


@pytest.fixture
def st(repo):
    s = BisectState(id="inv1", repo=str(repo), bad_sha=repo.shas[2])
    s.culprit = CulpritReport(sha=repo.shas[2], checkpoint_intent="Prompt: add a BROKEN marker file",
                              failing_evidence=["failing cases: x1"])
    return s


def run(st, repo, model, **kw):
    return run_fixer(st, model=model, checker=FakeChecker(repo), **kw)


def test_first_attempt_passes(st, repo, tmp_path):
    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}))
    res = run(st, repo, m, out_dir=tmp_path / "out")
    assert res.passed and len(res.attempts) == 1
    assert res.branch == f"agentbisect/fix-{repo.shas[2][:8]}"
    assert git(repo, "rev-parse", res.branch) == res.verifier_input["commit"]
    assert res.verifier_input == {"repo": str(repo), "commit": res.attempts[0].commit}
    assert git(repo, "show", f"{res.branch}:marker.txt") == "fixed"
    assert "+fixed" in res.patch and (tmp_path / "out" / "fix-inv1.patch").read_text() == res.patch
    body = (tmp_path / "out" / "fix-inv1-pr.md").read_text()
    assert "AgentBisect report" in body and "+fixed" in body and "NOT verified" not in body
    assert st.fix_attempts == res.attempts and st.metrics["fix"]["passed"] is True
    assert git(repo, "branch", "--list", "main") and git(repo, "rev-parse", "main") == repo.shas[2]  # main untouched
    assert git(repo, "worktree", "list").count("\n") == 0  # worktree cleaned up


def test_failures_fed_to_next_attempt_and_stop_on_success(st, repo):
    m = FakeModel(reply({"path": "marker.txt", "content": "wrong"}), reply({"path": "marker.txt", "content": "still"}),
                  reply({"path": "marker.txt", "content": "fixed"}))
    res = run(st, repo, m)
    assert [a.passed for a in res.attempts] == [False, False, True] and res.passed
    assert "Previous attempt" not in m.prompts[0]
    assert "marker is 'wrong'" in m.prompts[1] and "+wrong" in m.prompts[1]
    assert "marker is 'still'" in m.prompts[2] and "Previous attempt 1" in m.prompts[2]
    assert "Previous attempt 2" in m.prompts[2]


def test_gives_up_after_three_and_keeps_best_branch(st, repo, tmp_path):
    m = FakeModel(*[reply({"path": "marker.txt", "content": f"w{i}"}) for i in range(5)])
    res = run(st, repo, m, out_dir=tmp_path)
    assert not res.passed and len(res.attempts) == 3 and len(m.replies) == 2
    assert res.verifier_input["commit"] == res.attempts[-1].commit
    assert "NOT verified" in (tmp_path / "fix-inv1-pr.md").read_text()
    assert st.metrics["fix"]["passed"] is False


def test_bad_replies_are_retried_with_the_error(st, repo):
    m = FakeModel("not json at all", reply({"path": "../../etc/passwd", "content": "x"}),
                  reply({"path": "marker.txt", "content": "fixed"}))
    res = run(st, repo, m)
    assert res.passed and [a.error == "" for a in res.attempts] == [False, False, True]
    assert "not valid JSON" in m.prompts[1]
    assert "protected path" in m.prompts[2]
    assert not (repo.path.parent / "etc").exists()


def test_all_attempts_unusable_yields_no_verifier_input(st, repo):
    res = run(st, repo, FakeModel("x", "y", "z"))
    assert not res.passed and res.verifier_input == {} and res.patch == "" and res.attempts[-1].error
    assert git(repo, "branch", "--list", "agentbisect/*") == ""


def test_noop_edit_is_rejected(st, repo):
    res = run(st, repo, FakeModel(*[reply({"path": "marker.txt", "content": "3"})] * 3))
    assert "changed nothing" in res.attempts[0].error


def test_fixer_knows_only_the_brief(st, repo):
    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}))
    run(st, repo, m)
    p = m.prompts[0].lower()
    assert "culprit diff" in p and "original intent" in p and "add a broken marker file" in p
    assert "ground_truth" not in p and "planted" not in p and "planted" not in m.systems[0].lower()
    assert "### marker.txt" in m.prompts[0]  # current contents of the touched file


def test_forbidden_paths_never_read_or_written(tmp_path, repo, st):
    for bad in ("evidence/ground_truth.json", ".env", ".git/config", "/abs", "a/../../b", "x/.env.local", "answer_key.json"):
        with pytest.raises(ValueError):
            apply_edits(tmp_path, [{"path": bad, "content": "x"}])
    for bad in ("tests/test_x.py", "evals/cases.jsonl", "pkg/test_agent.py", "conftest.py"):
        with pytest.raises(ValueError, match="tests/eval"):
            apply_edits(tmp_path, [{"path": bad, "content": "x"}])
    wt, outside = tmp_path / "wt", tmp_path / "outside"
    wt.mkdir()
    outside.mkdir()
    (wt / "link").symlink_to(outside)
    (wt / "flink.py").symlink_to(outside / "f.py")
    for p in ("link/f.py", "flink.py"):
        with pytest.raises(ValueError, match="symlink"):
            apply_edits(wt, [{"path": p, "content": "x"}])
    assert not (outside / "f.py").exists()
    apply_edits(wt, [{"path": "secrets_manager.py", "content": "ok"}])  # not an answer key


def test_apply_edits_is_atomic_and_blocks_oversize(tmp_path):
    with pytest.raises(ValueError):
        apply_edits(tmp_path, [{"path": "a.py", "content": "x"}, {"path": ".env", "content": "x"}])
    assert not (tmp_path / "a.py").exists()
    with pytest.raises(ValueError, match="too large"):
        apply_edits(tmp_path, [{"path": "big.py", "content": "x"}], blocked={"big.py"})
    (tmp_path / "d").write_text("file where a directory is needed")
    with pytest.raises(ValueError, match="could not apply"):
        apply_edits(tmp_path, [{"path": "d/e.py", "content": "x"}])


def protected_commit(repo):
    (repo.path / "evidence").mkdir()
    (repo.path / "evidence" / "ground_truth.json").write_text('{"planted": "R1"}')
    (repo.path / "a_safe.py").write_text("ok_marker = 1\n")
    (repo.path / "big.py").write_text("x = 1\n" * 5000)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c4")
    return git(repo, "rev-parse", "HEAD")


def test_brief_and_prompt_never_contain_protected_material(st, repo):
    c4 = protected_commit(repo)
    st.bad_sha, st.culprit.sha = c4, c4
    st.culprit.checkpoint_intent = "Prompt: tidy things\nsee evidence/ground_truth.json for the answer\nkeep going"
    st.culprit.failing_evidence = ["failing cases: x1", "planted bug R1 lives in a_safe.py"]
    brief = fixer_brief(st.repo, st.culprit)
    prompt, oversize = build_prompt(st.repo, st.culprit, c4, [FixAttempt(
        branch="b", attempt=1, failures=["answer_key says R1"], diff="+evidence/ground_truth.json", files=["a_safe.py"])])
    for text in (brief, prompt):
        assert "ground_truth" not in text and "R1" not in text and "planted" not in text
        assert "ok_marker" in text and "protected" in text and "keep going" in text
    assert "### a_safe.py" in prompt and oversize == {"big.py"} and "too large" in prompt


def test_oversize_file_edit_is_refused_then_retried(st, repo):
    c4 = protected_commit(repo)
    st.bad_sha, st.culprit.sha = c4, c4
    m = FakeModel(reply({"path": "big.py", "content": "x = 2\n"}), reply({"path": "a_safe.py", "content": "fixed"}))

    class Chk:
        def check(self, commit, attempt):
            return True, []
    res = run_fixer(st, model=m, checker=Chk())
    assert "too large" in res.attempts[0].error and res.passed and "too large" in m.prompts[1]


def test_parse_reply_accepts_fenced_json_and_rejects_bad_shapes():
    expl, files = parse_reply("sure:\n```json\n" + reply({"path": "a", "content": "b"}, expl="why") + "\n```")
    assert expl == "why" and files == [{"path": "a", "content": "b"}]
    for bad in ('{"files": []}', '{"files": [{"path": "a"}]}', '{"files": "x"}', "{"):
        with pytest.raises(ValueError):
            parse_reply(bad)
    assert parse_reply(reply({"path": "a", "delete": True}))[1][0]["delete"] is True


def test_delete_edit(st, repo):
    class Chk:
        def check(self, commit, attempt):
            return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}:BROKEN"]).returncode != 0, []
    res = run_fixer(st, model=FakeModel(reply({"path": "BROKEN", "delete": True})), checker=Chk())
    assert res.passed and "-x" in res.patch


def test_pr_not_opened_by_default_and_draft_when_asked(st, repo):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/pull/7\n", stderr="")

    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}))
    res = run(st, repo, m, run=fake_run)
    assert calls == [] and res.pr_url is None
    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}))
    res = run(st, repo, m, open_pr=True, run=fake_run)
    assert res.pr_url == "https://github.com/o/r/pull/7"
    assert calls[0][-4:] == ["push", "-u", "origin", res.branch]
    assert calls[1][:3] == ["gh", "pr", "create"] and "--draft" in calls[1]
    assert calls[1][calls[1].index("--head") + 1] == res.branch
    assert calls[1][calls[1].index("--title") + 1].startswith("[DRAFT")


def test_failed_fix_is_never_pushed(st, repo):
    calls = []
    m = FakeModel(*[reply({"path": "marker.txt", "content": "nope"})] * 3)
    res = run(st, repo, m, open_pr=True, run=lambda cmd, **kw: calls.append(cmd))
    assert not res.passed and calls == [] and res.pr_url is None and res.verifier_input  # branch kept, not pushed


def test_pr_failure_is_recorded_not_raised(st, repo):
    bad = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="denied")  # noqa: E731
    res = run(st, repo, FakeModel(reply({"path": "marker.txt", "content": "fixed"})), open_pr=True, run=bad)
    assert res.passed and res.pr_url is None and "push failed" in res.pr_error
    assert st.metrics["fix"]["pr_error"] == res.pr_error and res.verifier_input["commit"]


def test_error_after_a_committed_attempt_keeps_that_attempt(st, repo):
    m = FakeModel(reply({"path": "marker.txt", "content": "w1"}), "garbage", "garbage")
    res = run(st, repo, m)
    assert [a.commit != "" for a in res.attempts] == [True, False, False] and not res.passed
    assert res.verifier_input["commit"] == res.attempts[0].commit == git(repo, "rev-parse", res.branch)
    assert res.patch == res.attempts[0].diff


def test_max_attempts_validated(st, repo):
    with pytest.raises(ValueError, match="at least 1"):
        run(st, repo, FakeModel(), max_attempts=0)
    m = FakeModel(*[reply({"path": "marker.txt", "content": "n"})] * 3)
    assert len(run(st, repo, m, max_attempts=1).attempts) == 1


# ---- CLI

def cli_setup(tmp_path, repo, monkeypatch, replies, culprit=True, passes=True):
    from agentbisect import bisect, fixer
    store = StateStore(tmp_path / "ab")
    s = store.create(str(repo), bad_sha=repo.shas[2], status="culprit_found")
    if culprit:
        s.culprit = CulpritReport(sha=repo.shas[2], checkpoint_intent="Prompt: add BROKEN")
        store.save(s)
    model = _Boom() if replies is None else FakeModel(*replies)
    monkeypatch.setattr(fixer, "ProxyFixModel", lambda model=None, _m=model: _m)
    monkeypatch.setattr(bisect, "make_runner", lambda store, st: None)
    monkeypatch.setattr(fixer, "RunnerChecker", lambda runner, run_id: FakeChecker(repo) if passes else _NeverChecker())
    return store, s, model


class _NeverChecker:
    def check(self, commit, attempt):
        return False, ["still failing"]


class _Boom:
    def complete(self, system, prompt):
        raise RuntimeError("model down")


def fix_cmd(tmp_path, *extra, id_):
    return cli.main(["--state-dir", str(tmp_path / "ab"), "fix", id_, *extra])


def test_cli_fix_pass(tmp_path, repo, monkeypatch, capsys):
    store, s, _ = cli_setup(tmp_path, repo, monkeypatch, [reply({"path": "marker.txt", "content": "fixed"})])
    assert fix_cmd(tmp_path, id_=s.id) == 0
    out = capsys.readouterr().out
    assert "verifier_input" in out and "pr: not opened" in out
    st = store.load(s.id)
    assert st.status == "verifying" and st.fix_attempts[0].passed and st.metrics["fix"]["passed"]
    assert (tmp_path / "ab" / "fixes" / f"fix-{s.id}.patch").exists()


def test_cli_fix_failure_returns_to_culprit_found_and_can_retry(tmp_path, repo, monkeypatch):
    store, s, _ = cli_setup(tmp_path, repo, monkeypatch, [reply({"path": "marker.txt", "content": "n"})] * 6,
                            passes=False)
    assert fix_cmd(tmp_path, "--max-attempts", "2", id_=s.id) == 1
    assert store.load(s.id).status == "culprit_found" and len(store.load(s.id).fix_attempts) == 2
    assert fix_cmd(tmp_path, "--max-attempts", "1", id_=s.id) == 1  # re-run is allowed
    assert len(store.load(s.id).fix_attempts) == 1


def test_cli_fix_crash_marks_failed_and_retry_works(tmp_path, repo, monkeypatch, capsys):
    store, s, _ = cli_setup(tmp_path, repo, monkeypatch, None)
    assert fix_cmd(tmp_path, id_=s.id) == 1
    st = store.load(s.id)
    assert st.status == "failed" and "model down" in (st.error or "")
    from agentbisect import fixer
    monkeypatch.setattr(fixer, "ProxyFixModel", lambda model=None: FakeModel(
        reply({"path": "marker.txt", "content": "fixed"})))
    assert fix_cmd(tmp_path, id_=s.id) == 0 and store.load(s.id).status == "verifying"


def test_cli_fix_rejects_bad_input(tmp_path, repo, monkeypatch, capsys):
    store, s, _ = cli_setup(tmp_path, repo, monkeypatch, [], culprit=False)
    assert fix_cmd(tmp_path, id_=s.id) == 1 and "no culprit" in capsys.readouterr().err
    assert fix_cmd(tmp_path, id_="nope") == 1 and "no investigation" in capsys.readouterr().err
    assert fix_cmd(tmp_path, "--max-attempts", "0", id_=s.id) == 2
    assert store.load(s.id).status == "culprit_found"


def test_report_agrees_with_fixer_result(st, repo):
    from agentbisect.report import best_attempt, fix_passes
    run(st, repo, FakeModel(reply({"path": "marker.txt", "content": "w"}), reply({"path": "marker.txt", "content": "fixed"})))
    assert best_attempt(st, None).attempt == 2 and fix_passes(best_attempt(st, None), None)


def test_fixresult_roundtrip_and_verifier_input_shape(st, repo):
    res = run(st, repo, FakeModel(reply({"path": "marker.txt", "content": "fixed"})))
    again = FixResult.from_dict(json.loads(json.dumps(res.to_dict())))
    assert again == res and set(again.verifier_input) == {"repo", "commit"}
    s2 = BisectState.from_dict(json.loads(json.dumps(st.to_dict())))
    assert s2.fix_attempts[0].commit == res.attempts[0].commit


def test_runner_checker_formats_failures():
    class R:
        def evaluate_commit(self, sha, run_id):
            return CommitResult(verdict="bad", pass_rate=0.25, failing_cases=["rw1"],
                                failure_signatures=[{"key": "call_args:x.y", "expected": "64", "got": "6400"}])
    ok, lines = RunnerChecker(R(), "i").check("abc", 2)
    assert not ok and any("rw1" in x for x in lines) and any("expected 64 got 6400" in x for x in lines)

    class G:
        def evaluate_commit(self, sha, run_id):
            return CommitResult(verdict="good", pass_rate=1.0)
    assert RunnerChecker(G(), "i").check("abc", 1) == (True, [])


def test_proxy_model_uses_pinned_model_and_joins_text():
    seen = {}

    class P:
        def messages(self, body, salt=""):
            seen.update(body)
            return {"content": [{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}]}
    m = ProxyFixModel(P(), model="claude-sonnet-5-5")
    assert m.complete("sys", "hi") == "abcd" and seen["model"] == "claude-sonnet-5-5" and seen["system"] == "sys"


# ---- intent check inside the Fixer's per-attempt check

class _Judge:
    def __init__(self, *verdicts):
        self.verdicts, self.calls = list(verdicts), []

    def judge(self, intent, culprit_diff, fix_diff):
        from agentbisect.verify import IntentVerdict
        self.calls.append((intent, fix_diff))
        v = self.verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return IntentVerdict(v, "reverts the intent of the change" if not v else "intent kept", "claude-sonnet-5-5")


def test_revert_that_passes_checks_fails_on_intent_and_reason_feeds_next_attempt(st, repo):
    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}),
                  reply({"path": "marker.txt", "content": "fixed"}, {"path": "other.txt", "content": "x"}))
    j = _Judge(False, True)
    res = run(st, repo, m, judge=j)
    a1, a2 = res.attempts
    assert a1.passed is False and a1.intent_test_passed is False
    assert any("intent not preserved" in f and "reverts the intent" in f for f in a1.failures)
    assert "reverts the intent of the change" in m.prompts[1] and "undid the intent" in m.prompts[1]
    assert a2.passed is True and a2.intent_test_passed is True and res.passed
    assert "BROKEN" in j.calls[0][0] and "marker.txt" in j.calls[0][1]  # intent + the attempt's own diff
    assert res.verifier_input["commit"] == a2.commit


def test_all_attempts_reverting_means_not_passed(st, repo):
    m = FakeModel(*[reply({"path": "marker.txt", "content": "fixed"}) for _ in range(3)])
    res = run(st, repo, m, judge=_Judge(False, False, False))
    assert not res.passed and [a.passed for a in res.attempts] == [False] * 3


def test_inconclusive_judge_does_not_block_but_is_recorded(st, repo):
    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}))
    res = run(st, repo, m, judge=_Judge(TimeoutError("judge down")))
    assert res.passed and res.attempts[0].intent_test_passed is None
    assert "judge down" in st.metrics["fix"]["intent_inconclusive"][0]["detail"]


def test_intent_not_judged_when_checks_fail(st, repo):
    j = _Judge()
    res = run(st, repo, FakeModel(reply({"path": "marker.txt", "content": "nope"})), judge=j, max_attempts=1)
    assert not res.passed and j.calls == []


def test_system_prompt_forbids_undoing_the_commit(st, repo):
    m = FakeModel(reply({"path": "marker.txt", "content": "fixed"}))
    run(st, repo, m)
    s = m.systems[0]
    assert "intent must be preserved" in s and "do not undo or revert the commit" in s and "CONSEQUENCE" in s


# ---- guarded eval-expectation edits

ROWS = [{"id": "r3", "category": "refund", "checks": {"call_args": {"amount": 64.0}}},
        {"id": "r4", "category": "refund", "checks": {"reply_regex": "ok"}},
        {"id": "h1", "category": "refund", "held_out": True, "checks": {"reply_regex": "ok"}}]


class _Pass:
    def check(self, commit, attempt):
        return True, []


class _EJudge(_Judge):
    def __init__(self, intent=True, edit=True):
        super().__init__(*([intent] * 5))
        self.edit, self.edit_calls = edit, []

    def judge_eval_edit(self, intent, culprit_diff, changes):
        from agentbisect.verify import IntentVerdict
        self.edit_calls.append((intent, changes))
        if isinstance(self.edit, Exception):
            raise self.edit
        return IntentVerdict(self.edit, "merely follows the contract change" if self.edit else "not implied by the intent",
                             "claude-sonnet-5-5")


@pytest.fixture
def st_cases(repo, st):
    from agentbisect.models import FailureSignature, RegressionSpec
    (repo.path / "evals").mkdir()
    (repo.path / "evals" / "cases.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS) + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "cases")
    st.bad_sha = git(repo, "rev-parse", "HEAD")
    st.spec = RegressionSpec(cases=["r3", "r4"], held_out=["h1"], signature=FailureSignature(
        key="call_args:issue_refund.amount", check="call_args", detail="issue_refund.amount", cases=["r3"]))
    return st


def edit_reply(case="r3", checks=None, reason="the commit moved the refund tool to integer cents", files=()):
    return json.dumps({"explanation": "follow the contract", "files": list(files),
                       "eval_edits": [{"case": case, "checks": {"call_args": {"amount_cents": 6400}} if checks is None else checks,
                                       "reason": reason}]})


def test_eval_edit_accepted_when_judge_confirms(st_cases, repo, tmp_path):
    j, m = _EJudge(True, True), FakeModel(edit_reply())
    res = run_fixer(st_cases, model=m, checker=_Pass(), judge=j, out_dir=tmp_path)
    assert res.passed and len(res.attempts) == 1 and len(j.edit_calls) == 1
    assert res.eval_expectation_changes == [{"case": "r3", "key": "call_args", "old": {"amount": 64.0},
                                             "new": {"amount_cents": 6400},
                                             "reason": "the commit moved the refund tool to integer cents"}]
    rows = [json.loads(x) for x in git(repo, "show", f"{res.branch}:evals/cases.jsonl").splitlines()]
    assert rows[0]["checks"] == {"call_args": {"amount_cents": 6400}} and rows[1:] == ROWS[1:]
    listed = m.prompts[0].split("Eval cases you may update")[1]
    assert '"r3"' in listed and '"r4"' not in listed and '"h1"' not in listed
    body = (tmp_path / "fix-inv1-pr.md").read_text()
    assert "Human review required: eval expectation changed" in body and "amount_cents" in body and "`r3`" in body
    assert st_cases.metrics["fix"]["eval_expectation_changes"][0]["case"] == "r3"
    again = FixResult.from_dict(json.loads(json.dumps(res.to_dict())))
    assert again.eval_expectation_changes == res.eval_expectation_changes


@pytest.mark.parametrize("edit,why", [(False, "not implied"), (TimeoutError("judge down"), "judge down")])
def test_eval_edit_rejected_when_judge_denies_or_is_inconclusive(st_cases, repo, edit, why):
    j = _EJudge(True, edit)
    m = FakeModel(edit_reply(), edit_reply())
    res = run_fixer(st_cases, model=m, checker=_Pass(), judge=j, max_attempts=2)
    assert not res.passed and [a.passed for a in res.attempts] == [False, False]
    assert why in m.prompts[1] and "eval expectation edit not accepted" in m.prompts[1]


def test_eval_edit_not_accepted_without_a_judge_or_with_bad_intent(st_cases, repo):
    assert not run_fixer(st_cases, model=FakeModel(edit_reply()), checker=_Pass(), judge=None, max_attempts=1).passed
    assert not run_fixer(st_cases, model=FakeModel(edit_reply()), checker=_Pass(), max_attempts=1,
                         judge=_Judge(TimeoutError("down"))).passed
    j = _EJudge(False, True)  # intent not preserved
    assert not run_fixer(st_cases, model=FakeModel(edit_reply()), checker=_Pass(), judge=j, max_attempts=1).passed
    assert j.edit_calls == []


@pytest.mark.parametrize("case", ["r4", "h1", "nope"])
def test_eval_edit_touching_non_target_case_is_rejected(st_cases, repo, case):
    m = FakeModel(edit_reply(case=case), edit_reply(case=case))
    j = _EJudge(True, True)
    res = run_fixer(st_cases, model=m, checker=_Pass(), judge=j, max_attempts=2)
    assert not res.passed and res.verifier_input == {} and j.edit_calls == []
    assert "eval edit refused" in res.attempts[0].error
    assert git(repo, "branch", "--list", "agentbisect/*") == ""


def test_eval_edit_needs_reason_and_checks_object(st_cases, repo):
    for bad in (edit_reply(reason=" "), edit_reply(checks={})):
        res = run_fixer(st_cases, model=FakeModel(bad), checker=_Pass(), judge=_EJudge(), max_attempts=1)
        assert not res.passed and "needs a string" in res.attempts[0].error


def test_eval_edit_that_changes_nothing_is_rejected(st_cases, repo):
    r = edit_reply(checks={"call_args": {"amount": 64.0}})
    res = run_fixer(st_cases, model=FakeModel(r), checker=_Pass(), judge=_EJudge(), max_attempts=1)
    assert not res.passed and "changed nothing" in res.attempts[0].error


def test_eval_file_still_refused_as_a_plain_file_edit(st_cases, repo):
    r = reply({"path": "evals/cases.jsonl", "content": "{}"})
    res = run_fixer(st_cases, model=FakeModel(r), checker=_Pass(), judge=_EJudge(), max_attempts=1)
    assert not res.passed and "tests/eval" in res.attempts[0].error


def test_code_edit_plus_eval_edit_and_code_only_unaffected(st_cases, repo):
    r = edit_reply(files=[{"path": "marker.txt", "content": "fixed"}])
    res = run_fixer(st_cases, model=FakeModel(r), checker=FakeChecker(repo), judge=_EJudge(True, True))
    assert res.passed and res.attempts[0].files == ["marker.txt"] and res.eval_expectation_changes
    j = _EJudge(True, True)
    code = run_fixer(st_cases, model=FakeModel(reply({"path": "marker.txt", "content": "fixed"})),
                     checker=FakeChecker(repo), judge=j)
    assert code.passed and code.eval_expectation_changes == [] and j.edit_calls == []
