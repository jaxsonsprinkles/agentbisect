import subprocess

import pytest

from agentbisect.cli import main
from agentbisect.models import (BisectState, CommitResult, CulpritReport, FixAttempt, RegressionSpec)
from agentbisect.report import (PR_FOOTER, build_report, open_pr, render_html, render_markdown, write_reports)
from agentbisect.state import StateStore

CULPRIT = "c0ffee1234567890abcdef1234567890abcdef12"
GOOD = "a" * 40


def make_state(*, fix=True, fix_ok=True, checkpoint=True, low=False, intent="Add cancel_order for unshipped orders"):
    st = BisectState(id="inv1", repo="/nonexistent-repo", good_sha=GOOD, bad_sha="b" * 40, status="culprit_found")
    st.spec = RegressionSpec(bad_trace_id="trace-bad-1", good_trace_ids=["trace-good-1"], cases=["s1"])
    st.results = {GOOD: CommitResult("good", 0.95, ["t-g"], repeats=3),
                  CULPRIT: CommitResult("bad", 0.40, ["t-c1", "t-c2"], repeats=3)}
    st.culprit = CulpritReport(
        sha=CULPRIT, checkpoint_intent=intent, semantic_diff="added ToolBox._tool_cancel_order (3 callers)",
        impacted_symbols=["ToolBox._tool_cancel_order", "SYSTEM_PROMPT"], revert_test_passed=False if low else True,
        confidence="low" if low else "high", intent_source="checkpoint" if checkpoint else "commit_message",
        trace_divergence=["step 2: good called lookup_order, bad called cancel_order"],
        flags=["multiple regressions likely"] if low else [])
    st.metrics = {"strategy": "smart", "eval_rounds": 14, "cost_usd": 0.028,
                  "revert_test": {"passed": not low, "baseline_pass_rate": 0.95, "culprit_pass_rate": 0.4,
                                  "reverted_pass_rate": 0.5 if low else 0.94, "eval_rounds": 4},
                  "comparison": {"plain": {"eval_rounds": 30, "cost_usd": 0.06},
                                 "smart": {"eval_rounds": 14, "cost_usd": 0.028}, "saved_rounds": 16}}
    st.context_tokens_per_step = [{"step": 1, "label": "plan:graph", "tokens": 850}]
    if fix:
        st.fix_attempts = [FixAttempt(branch="agentbisect/fix-1", diff="--- a\n+++ b\n-x\n+y",
                                      eval_score=0.95 if fix_ok else 0.6, held_out_score=0.93 if fix_ok else 0.5,
                                      intent_test_passed=fix_ok, trace_ids=["t-fix"])]
    return st


def text(st, **kw):
    return render_markdown(build_report(st, repo="/nonexistent-repo"), **kw)


def test_full_culprit_case():
    st = make_state()
    md, h = text(st), render_html(build_report(st, repo="/nonexistent-repo"))
    for s in (CULPRIT, "Original intent (from the Entire checkpoint", "Add cancel_order for unshipped orders",
              "_tool_cancel_order", "good called lookup_order, bad called cancel_order", "PASSED",
              "good commit 95%, culprit 40%, HEAD with culprit reverted 94%", "saved 16 eval rounds",
              "plan:graph", "850", "`trace-bad-1`", "`t-fix`", f"entire checkpoint explain {CULPRIT} --full",
              "Confidence: high", "pass rate after (fix) | 95%", "held-out score | 93%", "intent test | passed",
              "**verified**"):
        assert s in md, s
    assert "multiple regressions" not in md and "NOT verified" not in md
    assert "<html" in h and "http" not in h.replace("https://app.neatlogs.com", "")  # no external assets
    assert "<link" not in h and "<script" not in h and CULPRIT in h and "trace-bad-1" in h


def test_no_fix_renders_not_attempted():
    st = make_state(fix=False)
    assert "Fix: not attempted." in text(st)
    assert "Fix: not attempted." in render_html(build_report(st))
    with pytest.raises(ValueError):
        open_pr(st, "/nonexistent-repo")


def test_no_checkpoint_is_labelled():
    st = make_state(checkpoint=False, intent="just the commit message")
    md = text(st)
    assert "NO CHECKPOINT" in md and "commit message only" in md
    assert "none found" in md


def test_low_confidence_warning():
    st = make_state(low=True, fix_ok=False)
    md = text(st)
    assert "Confidence: low" in md and "Warning: multiple regressions likely" in md and "FAILED" in md
    h = render_html(build_report(st))
    assert "Warning: multiple regressions likely" in h and "NOT verified" in h


def test_html_escapes_commit_text_and_markdown_fences():
    evil = '<script>alert(1)</script> "q" & ```` end'
    st = make_state(intent=evil)
    st.culprit.semantic_diff = "<img src=x onerror=alert(1)>"
    st.culprit.trace_divergence = ["<b>bold</b>"]
    st.culprit.flags.append("<i>flag</i>")
    h = render_html(build_report(st))
    assert "<script>alert" not in h and "&lt;script&gt;alert(1)&lt;/script&gt;" in h
    assert "<img src=x" not in h and "<b>bold</b>" not in h and "<i>flag</i>" not in h
    md = text(st)
    assert "`````" in md  # fence widened past the 4-backtick run in the commit text


def test_pr_dry_run_never_executes_and_confirm_runs_stubbed_gh():
    st = make_state(fix_ok=False)
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("must not run")

    out = []
    res = open_pr(st, "/nonexistent-repo", run=boom, say=out.append)
    assert not calls and not res["executed"] and res["draft"] is True
    assert res["title"].startswith("[DRAFT") and res["body"].rstrip().endswith(PR_FOOTER)
    assert any("DRY RUN" in o for o in out)
    assert res["commands"][1][:3] == ["gh", "pr", "create"] and "--draft" in res["commands"][1]

    seen = []

    def stub(cmd, **k):
        seen.append((cmd, k.get("input")))
        return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/x/y/pull/1\n", stderr="")

    ok = make_state()
    res = open_pr(ok, "/nonexistent-repo", confirm=True, run=stub, say=lambda s: None)
    assert res["executed"] and res["url"].endswith("/pull/1") and not res["draft"]
    assert [c[0][0] for c in seen] == ["git", "gh"] and seen[1][1].rstrip().endswith(PR_FOOTER)
    assert "--draft" not in seen[1][0]


def test_pr_failure_surfaces():
    def fail(cmd, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied")
    with pytest.raises(RuntimeError, match="denied"):
        open_pr(make_state(), "/nonexistent-repo", confirm=True, run=fail, say=lambda s: None)


def test_cli_report_writes_files_and_gates_pr(tmp_path, capsys):
    store = StateStore(tmp_path / "s")
    st = make_state()
    st.id = store.create("/nonexistent-repo").id
    store.save(st)
    out = tmp_path / "out"
    assert main(["--state-dir", str(tmp_path / "s"), "report", st.id, "--out", str(out), "--format", "html"]) == 0
    assert (out / f"report-{st.id}.html").exists() and not (out / f"report-{st.id}.md").exists()
    assert main(["--state-dir", str(tmp_path / "s"), "report", st.id, "--out", str(out), "--confirm-open-pr"]) == 2
    assert main(["--state-dir", str(tmp_path / "s"), "report", st.id, "--out", str(out), "--open-pr"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert len(write_reports(st, out, "both")) == 2
