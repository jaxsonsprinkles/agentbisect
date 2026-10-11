import json

import pytest

from agentbisect import bisect as B
from agentbisect import cli
from agentbisect import planner as P
from agentbisect.intake import FixtureSource, IntakeResult, Span, span_from_dict
from agentbisect.models import CommitResult, RegressionSpec
from agentbisect.runner import Runner, RunnerConfig
from agentbisect.state import StateStore
from conftest import git
from test_bisect import PINS, FakeExec, SimulatedCrash, culprit_at

N, CULPRIT, DECOY_A, DECOY_B = 30, 21, 12, 25
CP_TEXT = {
    CULPRIT: "Make the agent act faster on refund requests by calling refund_order directly",
    DECOY_A: "Add structured logging to tool calls",
    DECOY_B: "Tidy the lookup_order docstring",
}


def build_repo(tmp_path, culprit=CULPRIT, touching=(DECOY_A, CULPRIT, DECOY_B)):
    r = tmp_path / "target"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    shas = []
    for i in range(N):
        (r / "marker.txt").write_text(str(i))
        msg = f"c{i}: housekeeping"
        if i == 0:
            (r / "tools.py").write_text("def lookup_order(): pass\n")
            (r / "other.py").write_text("")
        elif i in touching:
            with open(r / "tools.py", "a") as f:
                f.write(f"# change {i} {'refund_order' if i == culprit else ''}\n")
            msg = {CULPRIT: "Auto-refund when customer mentions refund", DECOY_A: "Add logging",
                   DECOY_B: "Tidy docstring"}.get(i, msg)
        else:
            with open(r / "other.py", "a") as f:
                f.write(f"# {i}\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", msg)
        shas.append(git(r, "rev-parse", "HEAD"))
    return r, shas


class FakeGraph:
    def __init__(self, files=("tools.py",), symbols=("refund_order",), fail=False):
        self.files, self.symbols, self.fail, self.calls = list(files), list(symbols), fail, []

    def impact(self, symbol):
        self.calls.append(("impact", symbol))
        if self.fail:
            raise P.GraphUnavailable("graph unavailable: not installed")
        if symbol not in ("refund_order", "_tool_refund_order"):
            return P.Impact(symbol, [], [], 120)
        return P.Impact(symbol, self.files, self.symbols, 800)

    def commit_entities(self, sha):
        self.calls.append(("commit", sha))
        if self.fail:
            raise P.GraphUnavailable("graph unavailable")
        msg = git(self.repo, "log", "-1", "--format=%B", sha)
        names = ["refund_order"] if "Auto-refund" in msg else ["something_else"]
        return [P.Entity("tools.py", n, "body_changed") for n in names], 200

    repo = None


class FakeCheckpoints:
    def __init__(self, shas, texts=CP_TEXT, only=None):
        self.map = {shas[i]: t for i, t in texts.items() if only is None or i in only}
        self.calls = 0

    def intent(self, sha):
        self.calls += 1
        t = self.map.get(sha)
        return P.Intent(t, "checkpoint", "CP" + sha[:6], len(t)) if t else None


def spans_fixture():
    good = [Span("llm", "llm", ["lookup_order"], {"order": "1"}), Span("llm", "llm", ["search_help"])]
    bad = [Span("llm", "llm", ["refund_order"], {"order": "1"}), Span("llm", "llm", ["search_help"])]
    return FixtureSource({"traces": [], "spans": {
        "good": [vars(s) | {"tools": s.tools} for s in good], "bad": [vars(s) | {"tools": s.tools} for s in bad]}})


def make(tmp_path, ex=None, culprit=CULPRIT, touching=(DECOY_A, CULPRIT, DECOY_B), graph=None, cps="default",
         scorer=None, source="default"):
    repo, shas = build_repo(tmp_path, culprit, touching)
    graph = graph or FakeGraph()
    graph.repo = str(repo)
    cps = FakeCheckpoints(shas) if cps == "default" else cps
    store = StateStore(tmp_path / "ab")
    ex = ex or FakeExec(culprit_at(culprit))
    runner = Runner(repo, RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS), backoff=0.0),
                    store.cache(str(repo)), ex, sleep=lambda s: None)
    spec = RegressionSpec(cases=["a", "b"], bad_trace_id="bad", good_trace_ids=["good"])
    st = store.create(str(repo), spec=spec, good_sha=shas[0], bad_sha=shas[-1])
    st.metrics["strategy"] = "smart"
    planner = P.Planner(str(repo), source=spans_fixture() if source == "default" else source, graph=graph,
                        checkpoints=cps, scorer=scorer, symptom="agent refunds instead of looking up the order")
    return repo, shas, store, runner, st, planner, ex


def test_smart_beats_plain_in_rounds(tmp_path):
    repo, shas, store, runner, st, planner, ex = make(tmp_path)
    out = B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    st = store.load(st.id)
    assert out.sha == shas[CULPRIT] and st.culprit.sha == shas[CULPRIT]
    assert st.plan.mode == "smart" and st.plan.candidates[0].sha == shas[CULPRIT]
    assert st.plan.divergent_span == "step 0: bad run chose refund_order instead of lookup_order"
    assert "refund_order" in st.plan.impacted_symbols
    c0 = st.plan.candidates[0]
    assert c0.score > st.plan.candidates[1].score and "changes impacted symbol" in c0.reason and "checkpoint" in c0.reason
    tested = [shas.index(s) for s in st.results]
    assert CULPRIT in tested and CULPRIT - 1 in tested
    assert st.metrics["planner"]["graph_available"] and not st.metrics["planner"].get("fell_back_to_plain")
    # same regression, plain bisect with a fresh cache
    store2 = StateStore(tmp_path / "ab2")
    st2 = store2.create(str(repo), spec=st.spec, good_sha=shas[0], bad_sha=shas[-1])
    r2 = Runner(repo, RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS)),
                store2.cache(str(repo)), FakeExec(culprit_at(CULPRIT)))
    assert B.run_investigation(store2, st2, r2).sha == shas[CULPRIT]
    assert st.metrics["eval_rounds"] < st2.metrics["eval_rounds"]


def test_context_tokens_logged_with_and_without_graph(tmp_path):
    repo, shas, store, runner, st, planner, ex = make(tmp_path)
    B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    st = store.load(st.id)
    labels = {e["label"]: e["tokens"] for e in st.context_tokens_per_step}
    assert any(k.startswith("plan:graph:impact") for k in labels) and "plan:without_graph" in labels
    ct = st.metrics["planner"]["context_tokens"]
    assert ct["with_graph"] > 0 and ct["without_graph"] > 0  # toy repo; real-repo ratio is in the dry check


def test_misranked_candidates_still_find_culprit_within_plain_budget(tmp_path):
    # culprit (17) touches no impacted file; the ranked decoys point the wrong way
    repo, shas, store, runner, st, planner, ex = make(tmp_path, culprit=17, touching=(DECOY_A, 19, DECOY_B))
    out = B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    st = store.load(st.id)
    assert out.sha == shas[17] and st.status == "culprit_found"
    assert st.metrics["eval_rounds"] <= (2 + P.plain_worst_case(len(shas) - 1) + 1) * 4  # never beyond plain's bound


def test_clean_candidate_is_reranked_to_next(tmp_path):
    # a decoy ranks first (its intent talks about refunds) but is good; the real culprit is next
    texts = {CULPRIT: "Tweak refund wording", DECOY_A: "Make the agent refund orders: call refund_order for refund requests"}
    repo, shas = build_repo(tmp_path)
    store = StateStore(tmp_path / "ab")
    g = FakeGraph()
    g.repo = str(repo)
    st = store.create(str(repo), spec=RegressionSpec(cases=["a", "b"]), good_sha=shas[0], bad_sha=shas[-1])
    st.metrics["strategy"] = "smart"
    runner = Runner(repo, RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS)),
                    store.cache(str(repo)), FakeExec(culprit_at(CULPRIT)))
    pl = P.Planner(str(repo), source=spans_fixture(), graph=g, checkpoints=FakeCheckpoints(shas, texts),
                   symptom="refund")
    pl.source = FixtureSource({"traces": []})  # no spans -> hints come from explicit symbols
    pl.symbol_hints = ["refund_order"]
    out = B.run_investigation(store, st, runner, P.SmartStrategy(pl, store))
    st = store.load(st.id)
    assert out.sha == shas[CULPRIT]
    assert st.plan.candidates[0].sha == shas[DECOY_A]


def test_no_checkpoint_falls_back_to_commit_message(tmp_path):
    cps = FakeCheckpoints([], {})  # nothing has a checkpoint
    repo, shas, store, runner, st, planner, ex = make(tmp_path, cps=cps)
    out = B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    st = store.load(st.id)
    assert out.sha == shas[CULPRIT]
    top = st.plan.candidates[0]
    assert top.sha == shas[CULPRIT] and "commit_message match" in top.reason and "Auto-refund" in top.reason


def test_graph_unavailable_uses_git_search_and_flags_it(tmp_path):
    repo, shas, store, runner, st, planner, ex = make(tmp_path, graph=FakeGraph(fail=True))
    out = B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    st = store.load(st.id)
    assert out.sha == shas[CULPRIT]
    info = st.metrics["planner"]
    assert info["graph_available"] is False and info["graph_unavailable"] is True and info["via"] == "git"
    assert "graph unavailable" in info["graph_unavailable_reason"]
    assert any("graph unavailable" in w for w in st.metrics["warnings"])
    assert st.plan.candidates[0].sha == shas[CULPRIT]
    assert any(e["label"].startswith("plan:graph_unavailable") for e in st.context_tokens_per_step)


def test_skipped_candidate_moves_to_next(tmp_path):
    repo, shas, store, runner, st, planner, ex = make(tmp_path)
    store.cache(str(repo)).set_skip(shas[DECOY_B], dict(PINS), "install failed")
    st.results[shas[CULPRIT]] = CommitResult("skip", reason="build broken")  # top candidate unusable
    out = B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    # culprit itself is skipped: ambiguous between it and neighbours, but the run must not crash or loop
    assert out.kind in ("unresolved", "culprit")


def test_crash_and_resume_reuses_plan(tmp_path):
    ex = FakeExec(culprit_at(CULPRIT), crash_after=10)
    repo, shas, store, runner, st, planner, _ = make(tmp_path, ex=ex)
    with pytest.raises(SimulatedCrash):
        B.run_investigation(store, st, runner, P.SmartStrategy(planner, store))
    disk = StateStore(tmp_path / "ab").load(st.id)
    assert disk.status == "running" and disk.in_flight and disk.metrics["planner"]["planned"]
    planned = [c.sha for c in disk.plan.candidates]
    cps2 = FakeCheckpoints(shas)
    pl2 = P.Planner(str(repo), source=None, graph=FakeGraph(), checkpoints=cps2)
    ex2 = FakeExec(culprit_at(CULPRIT))
    r2 = Runner(repo, RunnerConfig(cases=["a", "b"], repeats=2, install_cmd=None, models=dict(PINS)),
                store.cache(str(repo)), ex2)
    store.resume(disk)
    assert B.resume_investigation(store, disk, r2, P.SmartStrategy(pl2, store)) == 0
    final = store.load(st.id)
    assert final.culprit.sha == shas[CULPRIT] and cps2.calls == 0  # no re-planning
    assert [c.sha for c in final.plan.candidates] == planned


# -- units --

def test_first_divergence_kinds():
    t = lambda *names: [Span("llm", "llm", [n]) for n in names]
    assert P.first_divergence(t("a", "b"), t("a", "b")) is None
    d = P.first_divergence(t("lookup", "x"), t("lookup", "refund"))
    assert d.index == 1 and d.kind == "different_tool" and d.symbol_hints()[:2] == ["refund", "_tool_refund"]
    assert P.first_divergence(t("a"), t("a", "b")).kind == "extra_step"
    assert P.first_divergence(t("a", "b"), t("a")).kind == "missing_step"
    g, b = [Span("l", "llm", ["x"], {"id": 1})], [Span("l", "llm", ["x"], {"id": 2})]
    assert P.first_divergence(g, b).kind == "different_args"
    pg, pb = [Span("m", "llm")], [Span("m2", "llm")]  # no tools: prompt-driven
    assert P.first_divergence(pg, pb).symbol_hints()[0] == "prompt"


def test_span_from_dict_tool_shapes():
    assert span_from_dict({"name": "s", "output": {"content": [{"type": "tool_use", "name": "lookup", "input": {"a": 1}}]}}).tools == ["lookup"]
    oa = {"name": "s", "output": {"tool_calls": [{"function": {"name": "refund", "arguments": "{\"x\": 2}"}}]}}
    s = span_from_dict(oa)
    assert s.tools == ["refund"] and s.args == {"x": 2}
    assert span_from_dict({"name": "t", "attributes": {"tool.name": "search"}}).tools == ["search"]


EXPLAIN = """● Checkpoint 01M4KJSW7ZK3SZMTNMAEXEB6EV
  session  582738a6
  commits  b0be626 Take and return issue_refund amounts in integer cents to match Stripe
────────────────────────────────────────────────────────────
## Intent

You are a developer on this repo. Implement the following request, run the te...

## Summary

*Not generated yet. Run `entire checkpoint explain --generate X` to create an AI summary.*

## Files (2)

- `tests/test_agent.py`
- `tools/impl.py`

── Transcript (full session) ───────────────────────────────
[User] You are a developer on this repo. Implement the following request, run the tests, and commit.

Request: Return refund amounts in cents to match Stripe: issue_refund should take and return integer cents.

[Tool] Bash: List files

[Assistant] I've read the refund tool and its tests.

[Tool] Edit: /x/tools/impl.py
"""


def test_parse_real_checkpoint_shape():
    it = P.parse_checkpoint_explain(EXPLAIN)
    assert it.source == "checkpoint" and it.checkpoint_id == "01M4KJSW7ZK3SZMTNMAEXEB6EV"
    assert it.text.startswith("Return refund amounts in cents") and "tools/impl.py" in it.text
    assert "Not generated" not in it.text and "You are a developer" not in it.text
    assert P.parse_checkpoint_explain("commit abc has no Entire-Checkpoint trailer") is None
    assert P.parse_checkpoint_explain("checkpoint not found: zzz") is None


def test_entire_adapters_parse_real_json_shapes():
    impact = {"focus_matches_total": 3, "definitions": [
        {"qualified_name": "_refund", "file_path": "tests/test_agent.py"},
        {"qualified_name": "ToolBox._tool_issue_refund", "file_path": "tools/impl.py"},
        {"qualified_name": "S", "file_path": "evals/reference.py"}]}
    commit = {"files": [{"path": "tools/impl.py", "changes": [
        {"type": "signature_changed", "kind": "method", "name": "ToolBox._tool_issue_refund"}]}]}
    seen = []

    def run(argv, cwd):
        seen.append(argv)
        return (0, json.dumps(impact if "impact" in argv else commit), "")
    g = P.EntireGraph(".", run)
    imp = g.impact("issue_refund")
    assert imp.files == ["tools/impl.py"] and imp.symbols == ["ToolBox._tool_issue_refund"]  # fuzzy/test noise dropped
    assert "--head" in seen[0] and "--format" in seen[0]
    ents, n = g.commit_entities("abc")
    assert ents[0].name == "ToolBox._tool_issue_refund" and n > 0
    assert P.EntireGraph(".", lambda a, c: (127, "", "no such file")).impact("x") if False else True
    with pytest.raises(P.GraphUnavailable):
        P.EntireGraph(".", lambda a, c: (127, "", "no such file")).impact("x")
    with pytest.raises(P.GraphUnavailable):
        P.EntireGraph(".", lambda a, c: (0, "not json", "")).impact("x")
    assert P.EntireCheckpoints(".", lambda a, c: (0, EXPLAIN, "")).intent("b0be626").text
    assert P.EntireCheckpoints(".", lambda a, c: (1, "", "checkpoint not found")).intent("zz") is None


def test_scorers():
    docs = {"a": "Return refund amounts in cents for issue_refund", "b": "Add structured logging", "c": ""}
    s = P.LexicalScorer().score("agent calls refund tool wrongly", docs)
    assert s["a"] > s["b"] >= s["c"] == 0 and s == P.LexicalScorer().score("agent calls refund tool wrongly", docs)

    class FakeLLM:
        def complete(self, system, prompt, model):
            return "0.9" if "logging" in prompt else "no idea"
    ls = P.LLMScorer(FakeLLM(), "m").score("refund", docs)
    assert ls["b"] == 0.9 and ls["a"] == s_a(docs)


def s_a(docs):
    return P.LexicalScorer().score("refund", docs)["a"]  # unparseable reply -> lexical fallback


def test_cli_default_strategy_is_smart_and_compare(tmp_path, monkeypatch, capsys):
    repo, shas = build_repo(tmp_path)
    spec = RegressionSpec(cases=["a", "b"])
    monkeypatch.setattr(cli, "run_intake", lambda a: IntakeResult(spec, None, None, []))
    ex = FakeExec(culprit_at(CULPRIT))
    monkeypatch.setattr(B, "make_runner", lambda store, st: Runner(
        st.repo, RunnerConfig(cases=spec.cases, repeats=2, install_cmd=None, models=dict(PINS)),
        store.cache(st.repo), ex))
    sd = str(tmp_path / "ab")
    rc = cli.main(["--state-dir", sd, "investigate", "--trace", "t", "--repo", str(repo), "--good", shas[0]])
    assert rc == 0
    st = StateStore(sd).list()[0]
    assert st.metrics["strategy"] == "smart" and st.culprit.sha == shas[CULPRIT]
    assert st.metrics["planner"]["planned"]  # no trace spans in this fake intake: ranked by intent alone
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "compare", st.id]) == 0
    out = capsys.readouterr().out
    assert "plain" in out and "smart" in out and "same culprit: True" in out
