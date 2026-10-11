import json

from agentbisect import cli
from agentbisect import report as RP
from agentbisect import summary as SM
from agentbisect.models import CulpritReport, FailureSignature, RegressionSpec
from agentbisect.state import StateStore

BUDGET_RT = {"passed": True, "eval_rounds": 6, "culprit_pass_rate": 0.0, "reverted_pass_rate": 1.0, "budget": {
    "good": {"kind": "tokens", "median": 11873}, "repeats": 3,
    "before": {"kind": "tokens", "median": 31402, "within": 1, "total": 4},
    "after": {"kind": "tokens", "median": 12150, "within": 4, "total": 4}}}


def mk(store, key, **kw):
    spec = RegressionSpec(cases=["a"], signature=FailureSignature(key=key, check=key.split(":")[0]))
    st = store.create("/repo", spec=spec, good_sha="a" * 40, bad_sha="b" * 40)
    for k, v in kw.items():
        setattr(st, k, v)
    store.save(st)
    return st


def populate(tmp_path):
    store = StateStore(tmp_path / "ab")
    cmp_ = {"plain": {"eval_rounds": 24}, "smart": {"eval_rounds": 21}, "same_culprit": True}
    p = mk(store, "call_args:issue_refund.amount", status="culprit_found",
           culprit=CulpritReport(sha="1" * 40, confidence="medium", revert_test_passed=True),
           metrics={"revert_test": {"passed": True, "culprit_pass_rate": 0.0, "reverted_pass_rate": 1.0},
                    "comparison": cmp_})
    b = mk(store, "budget:tokens@order_status", status="culprit_found",
           culprit=CulpritReport(sha="2" * 40, confidence="high", revert_test_passed=True),
           metrics={"revert_test": BUDGET_RT, "signature_group": p.id})
    r = mk(store, "call_args:escalate_to_human.priority", status="refused",
           metrics={"known_preexisting": {"message": "x"}, "signature_group": p.id})
    f = mk(store, "budget:steps@x", status="failed", error="budget signature needs a token baseline | really",
           metrics={"signature_group": p.id})
    p.metrics["signature_siblings"] = [{"id": x.id, "key": x.spec.signature.key} for x in (b, r, f)]
    store.save(p)
    return store, p, b, r, f


def test_rows_are_derived_from_state(tmp_path):
    store, p, b, r, f = populate(tmp_path)
    rows = {x["id"]: x for x in SM.summary_rows(store.list())}
    assert rows[p.id]["outcome"] == "culprit " + "1" * 10 + " (confidence medium)"
    assert rows[p.id]["revert"] == "PASSED: pass 0% -> 100%" and rows[p.id]["rounds"] == "24 plain vs 21 smart"
    assert rows[b.id]["revert"] == ("PASSED: median tokens 31,402 -> 12,150, within budget 1/4 -> 4/4")
    assert rows[b.id]["rounds"] == "n/a (no compare data)"
    assert rows[r.id]["outcome"] == "known pre-existing failure — correctly refused" and rows[r.id]["revert"] == "not run"
    assert rows[f.id]["outcome"].startswith("failed: budget signature needs a token baseline")


def test_different_culprit_between_strategies_is_called_out(tmp_path):
    store, p, *_ = populate(tmp_path)
    p.metrics["comparison"]["same_culprit"] = False
    assert "DIFFERENT culprit" in SM.rounds_cell(p)


def test_text_md_json_renderings(tmp_path):
    store, p, *_ = populate(tmp_path)
    rows = SM.summary_rows(SM.group_states(store, store.load(p.id)))
    text = SM.render(rows, "text")
    assert text.splitlines()[0].startswith("signature") and "budget:tokens@order_status" in text
    assert len(text.splitlines()) == 2 + 4 and set(text.splitlines()[1]) <= {"-", " "}
    md = SM.render(rows, "md")
    assert md.splitlines()[0].startswith("| signature | outcome") and "\\|" in md  # the pipe in an error is escaped
    assert json.loads(SM.render(rows, "json"))[0]["signature"] == "call_args:issue_refund.amount"
    assert SM.render([], "text") == "no investigations"


def test_cli_summary_reads_a_state_dir_positional_and_group(tmp_path, capsys):
    store, p, b, *_ = populate(tmp_path)
    other = StateStore(tmp_path / "other")
    mk(other, "judge", status="running")
    assert cli.main(["--state-dir", str(tmp_path / "other"), "summary", str(store.base)]) == 0
    out = capsys.readouterr().out
    assert "budget:tokens@order_status" in out and "judge" not in out  # positional state dir wins
    assert cli.main(["--state-dir", str(store.base), "summary", "--id", b.id, "--format", "md",
                     "--out", str(tmp_path / "t.md")]) == 0
    assert capsys.readouterr().out.count("| call_args:") == 2 and (tmp_path / "t.md").read_text().startswith("| signature")


def test_group_states_from_primary_or_sibling(tmp_path):
    store, p, b, r, f = populate(tmp_path)
    assert [s.id for s in SM.group_states(store, store.load(p.id))] == [p.id, b.id, r.id, f.id]
    assert [s.id for s in SM.group_states(store, store.load(f.id))] == [p.id, b.id, r.id, f.id]


def test_report_contains_the_per_signature_table_for_a_group_only(tmp_path):
    store, p, *_ = populate(tmp_path)
    st = store.load(p.id)
    rep = RP.build_report(st, repo=None, group=SM.group_states(store, st))
    md, page = RP.render_markdown(rep), RP.render_html(rep)
    assert "## Per-signature results" in md and "budget:tokens@order_status" in md and "known pre-existing" in md
    assert "<h2>Per-signature results</h2>" in page and "median tokens 31,402 -&gt; 12,150" in page
    alone = RP.build_report(st, repo=None, group=[st])
    assert "Per-signature results" not in RP.render_markdown(alone) and alone["summary"] is None


def test_cli_report_writes_the_group_table(tmp_path, capsys):
    store, p, *_ = populate(tmp_path)
    assert cli.main(["--state-dir", str(store.base), "report", p.id, "--out", str(tmp_path / "out")]) == 0
    assert "Per-signature results" in (tmp_path / "out" / f"report-{p.id}.md").read_text()


def test_unknown_id_is_a_clean_error_and_json_cli(tmp_path, capsys):
    store, p, *_ = populate(tmp_path)
    assert cli.main(["--state-dir", str(store.base), "summary", "--id", "nope"]) == 1
    assert "error:" in capsys.readouterr().err
    assert cli.main(["--state-dir", str(store.base), "summary", "--format", "json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4


def test_no_spec_no_culprit_and_inconclusive_cells(tmp_path):
    store = StateStore(tmp_path / "ab")
    st = store.create("/repo")
    row = SM.summary_rows([st])[0]
    assert row["signature"] == "(whole-case pass rate)" and row["revert"] == "not run" and row["outcome"] == "created"
    st.culprit = CulpritReport(sha="3" * 40, revert_test_passed=None)
    st.metrics["revert_test"] = {}
    assert SM.revert_cell(st) == "inconclusive"
    st.culprit.revert_test_passed = False
    st.metrics["revert_test"] = {"culprit_pass_rate": 0.5, "reverted_pass_rate": 0.5}
    assert SM.revert_cell(st) == "FAILED: pass 50% -> 50%"


def test_table_is_in_the_no_culprit_report_too(tmp_path):
    store, p, b, r, f = populate(tmp_path)
    st = store.load(r.id)
    st.metrics["known_preexisting"] = {"signature": "k", "label": "l", "good_sha": "a" * 40, "good_pass_rate": 0.0,
                                       "failing_cases": ["es2"], "fails": 3, "runs": 3, "message": "m"}
    rep = RP.build_report(st, repo=None, group=SM.group_states(store, st))
    assert "## Per-signature results" in RP.render_markdown(rep)
    assert "Per-signature results" in RP.render_html(rep)
