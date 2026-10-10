"""Smart planner: rank candidate commits, test the likeliest first, fall back to plain bisect.

Pipeline (the plan's "smart bisect loop", steps 2-5):
  1. divergence: span-by-span diff of a good vs bad trace -> first divergent step -> `Plan.divergent_span`.
  2. impact: `entire graph impact` maps the divergent step (tool name / prompt) to code (files, symbols); commits in
     range that touch it are found with the file list and `entire graph commit` entity changes. When entire-graph is
     unavailable (or does not know the symbol) the fallback is `git grep` + path-touching commits + `git log -S`,
     flagged in `state.metrics["planner"]` ("graph unavailable").
  3. rank: each candidate is scored by how well its Entire checkpoint intent (`entire checkpoint explain <sha>
     --full`: the coding agent's request) matches the symptom; commits without a checkpoint use their commit message.
     Scorer is deterministic lexical by default; an LLM scorer can be injected (no live client ships here).
  4. strategy: test the top candidate, then its parent (engine confirms culprit = first bad whose parent is good);
     a clean candidate (good, or its parent is bad too) is dropped and the rest re-ranked; once nothing ranked is
     left in the open range, plain bisect finishes the job. Every decision is a pure function of the persisted plan
     and verdicts, so a crashed run resumes with a fresh strategy object.
Context accounting: bytes read by the planner are logged as tokens (bytes/4) with the graph (`plan:*` labels) and
the estimate of what reading the raw range diff would cost without it (`plan:without_graph`).
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .bisect import PlainStrategy, linear_history, narrow
from .intake import LLM, Span, TraceSource, judge_model
from .models import BisectState, Candidate, CommitResult, Plan
from .state import StateStore

Run = Callable[[list[str], str], "tuple[int, str, str]"]  # (argv, cwd) -> (rc, stdout, stderr)
GRAPH_UNAVAILABLE = "graph unavailable"
INTENT_CHARS = 4000


def tokens_of_bytes(n: int) -> int:
    return (max(n, 0) + 3) // 4


def _run(argv: list[str], cwd: str) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------- 1. divergence

@dataclass
class Divergence:
    index: int
    kind: str  # different_tool | different_args | extra_step | missing_step
    good: Span | None
    bad: Span | None

    def describe(self) -> str:
        g = ",".join(self.good.tools) or (self.good.name if self.good else "-")
        b = ",".join(self.bad.tools) or (self.bad.name if self.bad else "-")
        return {"different_tool": f"step {self.index}: bad run chose {b} instead of {g}",
                "different_args": f"step {self.index}: {b} called with different arguments than in the good run",
                "extra_step": f"step {self.index}: bad run made an extra step {b}",
                "missing_step": f"step {self.index}: bad run stopped before {g}"}[self.kind]

    def symbol_hints(self) -> list[str]:
        """Names to look up in the code graph: bad tool first (the changed behaviour), then the good one."""
        hints: list[str] = []
        for s in (self.bad, self.good):
            for t in (s.tools if s else []):
                hints += [t, f"_tool_{t}"]
        if not hints:  # not a tool step: the prompt / model call shaped it
            hints = ["prompt"] + [s.name for s in (self.bad, self.good) if s and s.name]
        return list(dict.fromkeys(hints))


def first_divergence(good: list[Span], bad: list[Span]) -> Divergence | None:
    """First step where the bad trace differs from the good one. If any span calls a tool only tool steps are
    compared (LLM bookkeeping spans differ run to run)."""
    if any(s.tools for s in good + bad):
        good, bad = [s for s in good if s.tools], [s for s in bad if s.tools]
    for i in range(max(len(good), len(bad))):
        g, b = (good[i] if i < len(good) else None), (bad[i] if i < len(bad) else None)
        if g is None:
            return Divergence(i, "extra_step", None, b)
        if b is None:
            return Divergence(i, "missing_step", g, None)
        if g.signature != b.signature:
            return Divergence(i, "different_tool", g, b)
        if g.tools and g.args != b.args:
            return Divergence(i, "different_args", g, b)
    return None


# ---------------------------------------------------------------- 2. graph / git adapters

class GraphUnavailable(RuntimeError):
    pass


@dataclass
class Impact:
    symbol: str
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)  # qualified names
    nbytes: int = 0


@dataclass
class Entity:
    path: str
    name: str  # qualified, e.g. ToolBox._tool_issue_refund
    change: str  # added | removed | body_changed | signature_changed ...


class GraphAdapter(Protocol):
    def impact(self, symbol: str) -> Impact: ...

    def commit_entities(self, sha: str) -> tuple[list[Entity], int]:
        """(entity changes of the commit vs its first parent, bytes read)."""


def _is_test_path(p: str) -> bool:
    low = p.lower()
    return low.startswith(("tests/", "test/")) or "/tests/" in low or low.rsplit("/", 1)[-1].startswith("test_")


class EntireGraph:
    """`entire graph` (plugin graph v0.4.0). Verified against support-agent-demo (Python): `impact --head --format
    json` returns `focus` (exact) or `definitions` (ambiguous/fuzzy), `callers.entries[].endpoint.file_path`;
    `commit <sha> --json` returns files[].changes[{type, kind, name}]. Impact queries the committed HEAD tree, not
    each historical commit, so it is only used to find *where* the step's code lives now."""

    def __init__(self, repo: str, run: Run = _run, binary: str = "entire"):
        self.repo, self.run, self.binary = repo, run, binary

    def _json(self, *args: str) -> tuple[Any, int]:
        rc, out, err = self.run([self.binary, "graph", *args], self.repo)
        if rc != 0:
            raise GraphUnavailable(f"{GRAPH_UNAVAILABLE}: entire graph {args[0]} rc={rc}: {(err or out).strip()[:200]}")
        try:
            return json.loads(out), len(out.encode())
        except ValueError as e:
            raise GraphUnavailable(f"{GRAPH_UNAVAILABLE}: non-JSON output from entire graph {args[0]}") from e

    def impact(self, symbol: str) -> Impact:
        d, n = self._json("impact", "--symbol", symbol, "--repo", ".", "--format", "json", "--head", "--exclude-tests")
        defs = [d["focus"]] if d.get("focus") else d.get("definitions") or []
        # fuzzy matching returns unrelated tokens ('S', 'one'): keep only real mentions of the symbol
        defs = [x for x in defs if symbol.lower() in str(x.get("qualified_name", "")).lower()]
        defs = [x for x in defs if x.get("file_path") and not _is_test_path(x["file_path"])]
        files = [x["file_path"] for x in defs]
        syms = [x["qualified_name"] for x in defs]
        if d.get("focus"):
            for e in (d.get("callers") or {}).get("entries") or []:
                ep = e.get("endpoint") or {}
                if ep.get("file_path") and not ep.get("external") and not _is_test_path(ep["file_path"]):
                    files.append(ep["file_path"])
                    syms.append(ep.get("qualified_name") or ep.get("name", ""))
        return Impact(symbol, list(dict.fromkeys(files)), list(dict.fromkeys(syms)), n)

    def commit_entities(self, sha: str) -> tuple[list[Entity], int]:
        d, n = self._json("commit", sha, "--json", "--repo", ".")
        ents = [Entity(f["path"], c.get("name", ""), c.get("type", ""))
                for f in d.get("files") or [] for c in f.get("changes") or []]
        return ents, n


class Git:
    def __init__(self, repo: str, run: Run = _run):
        self.repo, self.run = repo, run

    def out(self, *args: str) -> str:
        rc, out, err = self.run(["git", "-C", self.repo, *args], self.repo)
        if rc != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {err.strip()}")
        return out

    def files_of(self, sha: str) -> list[str]:
        return self.out("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha).split()

    def message(self, sha: str) -> str:
        return self.out("log", "-1", "--format=%B", sha).strip()

    def range_diff_bytes(self, good: str, bad: str) -> int:
        return len(self.out("diff", f"{good}..{bad}").encode())

    def files_defining(self, bad: str, symbol: str) -> list[str]:
        rc, out, _ = self.run(["git", "-C", self.repo, "grep", "-l", "-w", "-e", symbol, bad, "--"], self.repo)
        return [ln.split(":", 1)[1] for ln in out.splitlines() if ":" in ln and not _is_test_path(ln.split(":", 1)[1])]

    def pickaxe(self, good: str, bad: str, symbol: str) -> list[str]:
        """Commits in good..bad whose diff adds or removes `symbol` (git log -S)."""
        return self.out("log", "--first-parent", "--format=%H", "-S", symbol, f"{good}..{bad}").split()


# ---------------------------------------------------------------- 3. checkpoints + scoring

@dataclass
class Intent:
    text: str
    source: str  # checkpoint | commit_message
    checkpoint_id: str | None = None
    nbytes: int = 0


class CheckpointAdapter(Protocol):
    def intent(self, sha: str) -> Intent | None:
        """Checkpoint-derived intent for the commit, or None when it has no checkpoint."""


def parse_checkpoint_explain(text: str) -> Intent | None:
    """Parse `entire checkpoint explain <sha> --full` (verified shape: header `● Checkpoint <id>`, `## Intent`
    (truncated with '...'), `## Summary` (placeholder until generated), `## Files`, then a transcript whose first
    `[User]` block holds the coding agent's `Request:` and `[Assistant]` lines narrate the change)."""
    if "no Entire-Checkpoint trailer" in text or "checkpoint not found" in text or "● Checkpoint" not in text:
        return None
    cid = (re.search(r"● Checkpoint (\S+)", text) or [None, None])[1]
    head, _, transcript = text.partition("── Transcript")
    parts: list[str] = []
    user = re.search(r"\[User\](.*?)(?=\n\[(?:Tool|Assistant|User)\]|\Z)", transcript, re.S)
    if user:
        body = user.group(1).strip()
        m = re.search(r"Request:\s*(.*)", body, re.S)
        parts.append((m.group(1) if m else body).strip())
    sm = re.search(r"## Summary\s*\n+(.*?)(?=\n## |\n──|\Z)", head, re.S)
    if sm and "Not generated yet" not in sm.group(1):
        parts.append(sm.group(1).strip())
    if not parts:  # transcript missing: fall back to the (possibly truncated) intent header
        im = re.search(r"## Intent\s*\n+(.*?)(?=\n## |\n──|\Z)", head, re.S)
        if im:
            parts.append(im.group(1).strip())
    parts += [a.strip() for a in re.findall(r"\[Assistant\]\s*(.*)", transcript)][:6]
    parts += re.findall(r"^- `([^`]+)`", head, re.M)
    return Intent("\n".join(parts)[:INTENT_CHARS], "checkpoint", cid, len(text.encode()))


class EntireCheckpoints:
    def __init__(self, repo: str, run: Run = _run, binary: str = "entire"):
        self.repo, self.run, self.binary = repo, run, binary

    def intent(self, sha: str) -> Intent | None:
        rc, out, _ = self.run([self.binary, "checkpoint", "explain", sha, "--full"], self.repo)
        return parse_checkpoint_explain(out) if rc == 0 else None


_STOP = set("a an and are as at be but by for from has have in into is it its of on or that the this to was were "
            "with when while should would not no do does did we you our they their then than so if i can will".split())


def _terms(s: str) -> list[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s).replace("_", " ")
    out = []
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if w in _STOP or len(w) < 2:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


class Scorer(Protocol):
    def score(self, symptom: str, docs: dict[str, str]) -> dict[str, float]:
        """sha -> relevance in [0, 1]."""


class LexicalScorer:
    """Deterministic: idf-weighted share of the symptom's terms found in the intent text."""

    def score(self, symptom: str, docs: dict[str, str]) -> dict[str, float]:
        q = set(_terms(symptom))
        tok = {k: set(_terms(v)) for k, v in docs.items()}
        n = len(docs)
        df = {t: sum(t in ts for ts in tok.values()) for t in q}
        idf = {t: math.log(1 + (n + 1) / (df[t] + 0.5)) for t in q}
        total = sum(idf.values()) or 1.0
        return {k: round(sum(idf[t] for t in q if t in ts) / total, 4) for k, ts in tok.items()}


class LLMScorer:
    """Optional: asks an injected `LLM` (see intake.LLM) for a 0-1 relevance per commit. No live client ships; a
    reply without a number falls back to the lexical score for that commit."""

    def __init__(self, llm: LLM, model: str | None = None):
        self.llm, self.model, self.fallback = llm, model or judge_model(), LexicalScorer()

    def score(self, symptom: str, docs: dict[str, str]) -> dict[str, float]:
        lex = self.fallback.score(symptom, docs)
        out = {}
        for sha, text in docs.items():
            reply = self.llm.complete(
                "Rate 0 to 1 how likely a code change with the given intent caused the symptom. Reply with a number.",
                f"Symptom: {symptom}\n\nChange intent:\n{text[:INTENT_CHARS]}", self.model)
            m = re.search(r"\d*\.?\d+", reply or "")
            out[sha] = round(min(max(float(m.group()), 0.0), 1.0), 4) if m else lex[sha]
        return out


# ---------------------------------------------------------------- the planner

TEXT_WEIGHT, IMPACT_WEIGHT = 0.7, 0.3


class Planner:
    def __init__(self, repo: str, *, source: TraceSource | None = None, graph: GraphAdapter | None = None,
                 checkpoints: CheckpointAdapter | None = None, scorer: Scorer | None = None,
                 git: Git | None = None, symptom: str = "", symbol_hints: list[str] | None = None):
        self.repo, self.source = repo, source
        self.graph = graph if graph is not None else EntireGraph(repo)
        self.checkpoints = checkpoints if checkpoints is not None else EntireCheckpoints(repo)
        self.scorer = scorer or LexicalScorer()
        self.git = git or Git(repo)
        self.symptom, self.symbol_hints = symptom, symbol_hints
        self.reads: list[dict[str, Any]] = []  # {phase, label, bytes}

    def _read(self, phase: str, label: str, nbytes: int) -> None:
        self.reads.append({"phase": phase, "label": label, "bytes": nbytes})

    # -- phase 1
    def divergence(self, st: BisectState) -> Divergence | None:
        spec = st.spec
        if not self.source or not spec or not spec.bad_trace_id or not spec.good_trace_ids:
            return None
        bad = self.source.spans(spec.bad_trace_id)
        for gid in spec.good_trace_ids:
            good = self.source.spans(gid)
            if good and bad:
                self._read("divergence", gid, sum(len(s.text) + len(s.name) for s in good + bad))
                return first_divergence(good, bad)
        return None

    # -- phase 2
    def locate(self, hints: list[str], history: list[str]) -> tuple[dict[str, float], list[str], dict[str, Any]]:
        """-> ({sha: impact weight 0..1}, impacted symbols, info). Graph first, git fallback."""
        good, bad = history[0], history[-1]
        info: dict[str, Any] = {"graph_available": True, "graph_unavailable_reason": None, "via": "graph"}
        symbols: list[str] = []
        files: list[str] = []
        try:
            for h in hints:
                imp = self.graph.impact(h)
                self._read("impact", h, imp.nbytes)
                files += imp.files
                symbols += imp.symbols
        except GraphUnavailable as e:
            info.update(graph_available=False, graph_unavailable_reason=str(e), via="git")
            files, symbols = [], []
        files = list(dict.fromkeys(files))
        weights: dict[str, float] = {}
        if files:
            names = {s.rsplit(".", 1)[-1] for s in symbols} | {s for s in symbols}
            for sha in history[1:]:
                touched = set(self.git.files_of(sha)) & set(files)
                if not touched:
                    continue
                weights[sha] = 0.5
                try:
                    ents, n = self.graph.commit_entities(sha)
                except GraphUnavailable:
                    continue
                self._read("rank", f"graph commit {sha[:8]}", n)
                if any(e.path in touched and (e.name in names or e.name.rsplit(".", 1)[-1] in names) for e in ents):
                    weights[sha] = 1.0
        if not weights:  # graph down or silent: git grep -> path-touching commits, plus git log -S
            if info["via"] == "graph":
                info["via"] = "git"
                info["graph_empty"] = True
            gfiles: list[str] = []
            for h in hints:
                gfiles += self.git.files_defining(bad, h)
                for sha in self.git.pickaxe(good, bad, h):
                    weights[sha] = 1.0
                    self._read("fallback", f"git log -S {h}", 41)
            for sha in history[1:]:
                if sha not in weights and set(self.git.files_of(sha)) & set(gfiles):
                    weights[sha] = 0.5
            files, symbols = list(dict.fromkeys(gfiles)), symbols or list(hints)
        info["files"] = files
        return weights, list(dict.fromkeys(symbols)), info

    # -- phases 3 + plan assembly
    def plan(self, st: BisectState, history: list[str]) -> tuple[Plan, dict[str, Any]]:
        div = None
        try:
            div = self.divergence(st)
        except Exception as e:  # trace access must never kill the investigation (login prompt etc.)
            warn = f"divergence unavailable: {type(e).__name__}: {e}"
            st.metrics.setdefault("warnings", []).append(warn)
        hints = self.symbol_hints or (div.symbol_hints() if div else [])
        weights, symbols, info = (self.locate(hints, history) if hints else
                                  ({}, [], {"graph_available": True, "via": "none"}))
        range_shas = history[1:]
        pool = list(weights) if weights else range_shas  # no code signal: rank the whole range by intent alone
        symptom = " ".join(x for x in (self.symptom, div.describe() if div else "",
                                       " ".join(hints), (st.spec.cases and " ".join(st.spec.cases)) or "") if x)
        intents: dict[str, Intent] = {}
        for sha in pool:
            it = self.checkpoints.intent(sha)
            if it:
                self._read("rank", f"checkpoint {sha[:8]}", it.nbytes)
            else:
                msg = self.git.message(sha)
                it = Intent(msg, "commit_message", None, len(msg.encode()))
                self._read("rank", f"commit message {sha[:8]}", it.nbytes)
            intents[sha] = it
        text = self.scorer.score(symptom, {s: i.text for s, i in intents.items()})
        pos = {s: i for i, s in enumerate(history)}
        cands = []
        for sha in pool:
            w = weights.get(sha, 0.0)
            score = round(TEXT_WEIGHT * text.get(sha, 0.0) + IMPACT_WEIGHT * w, 4)
            where = {1.0: "changes impacted symbol", 0.5: "touches impacted file", 0.0: "no code link"}[w]
            it = intents[sha]
            snippet = " ".join(it.text.split())[:90]
            cands.append((score, pos[sha], Candidate(sha, score, f"{where}; {it.source} match {text.get(sha, 0.0):.2f}: {snippet}")))
        cands.sort(key=lambda c: (-c[0], -c[1]))  # best score first; newer commit wins ties
        plan = Plan(divergent_span=div.describe() if div else None, impacted_symbols=symbols,
                    candidates=[c for _, _, c in cands], mode="smart")
        info.update(hints=hints, graph_unavailable=not info.get("graph_available", True),
                    without_graph_bytes=self._baseline(history, intents))
        return plan, info

    def _baseline(self, history: list[str], intents: dict[str, Intent]) -> int:
        """What reading the raw range costs without the graph: the full diff, plus the intent text of every commit
        in range (the ones actually read count as-is, the rest at the average size of those)."""
        try:
            diff = self.git.range_diff_bytes(history[0], history[-1])
        except RuntimeError:
            return 0
        sizes = [i.nbytes for i in intents.values()]
        unread = len(history) - 1 - len(sizes)
        return diff + sum(sizes) + unread * (sum(sizes) // len(sizes) if sizes else 0)


# ---------------------------------------------------------------- 4. strategy

class SmartStrategy:
    """Plugs into bisect.Strategy. Plans once (persisted in `state.plan`, `metrics["planner"]`), then every
    decision is derived from the plan and recorded verdicts."""
    name = "smart"

    def __init__(self, planner: Planner, store: StateStore | None = None):
        self.planner, self.store = planner, store

    def _history(self, st: BisectState) -> list[str]:
        return linear_history(st.repo, st.good_sha, st.bad_sha)

    def _ensure_plan(self, st: BisectState, history: list[str]) -> None:
        if st.metrics.get("planner", {}).get("planned"):
            return
        plan, info = self.planner.plan(st, history)
        st.plan = plan
        st.metrics["planner"] = {**info, "planned": True, "candidates": len(plan.candidates)}
        if info.get("graph_unavailable"):
            st.metrics.setdefault("warnings", []).append(f"{GRAPH_UNAVAILABLE}: fell back to git search")
        self._log_context(st)
        if self.store:
            self.store.save(st)

    def _log_context(self, st: BisectState) -> None:
        by_phase: dict[str, int] = {}
        for r in self.planner.reads:
            by_phase[r["phase"]] = by_phase.get(r["phase"], 0) + r["bytes"]
        label = "plan:graph" if st.metrics["planner"].get("graph_available", True) else f"plan:{GRAPH_UNAVAILABLE.replace(' ', '_')}"
        entries = [(f"{label}:{p}", b) for p, b in by_phase.items()]
        entries.append(("plan:without_graph", st.metrics["planner"].pop("without_graph_bytes", 0)))
        for lab, b in entries:
            if self.store:
                self.store.log_context_tokens(st, lab, tokens_of_bytes(b))
            else:
                st.context_tokens_per_step.append({"step": st.step + 1, "label": lab, "tokens": tokens_of_bytes(b)})
        withg = sum(tokens_of_bytes(b) for b in by_phase.values())
        st.metrics["planner"]["context_tokens"] = {"with_graph": withg, "without_graph": tokens_of_bytes(
            next(b for lab, b in entries if lab == "plan:without_graph"))}

    def next_commit(self, st: BisectState) -> str | None:
        history = self._history(st)
        self._ensure_plan(st, history)
        n = narrow(history, st.results)
        if n.done:
            return None
        untested = {history[i]: i for i in n.untested}
        ranked = [c.sha for c in st.plan.candidates]
        # a candidate that came back bad: its parent decides whether it is the culprit
        if n.hi - 1 in n.untested and history[n.hi] in ranked:
            return history[n.hi - 1]
        for sha in ranked:  # re-ranking = the plan order filtered to what the verdicts left open
            if sha in untested:
                return sha
        if st.plan.mode != "plain":  # every ranked candidate was clean/skipped: plain bisect on what is left
            st.plan.mode = "plain"
            st.metrics["planner"]["fell_back_to_plain"] = True
        return PlainStrategy(history).next_commit(st)

    def update(self, st: BisectState, sha: str, result: CommitResult) -> None:
        pass


# ---------------------------------------------------------------- 5. compare

def compare_strategies(st: BisectState, base_dir: str, runner_factory, strategy_factory) -> dict[str, Any]:
    """Run plain then smart on copies of one investigation, each in its own state dir (so its verdict cache starts
    empty: neither strategy can reuse the other's runs). Returns {strategy: {rounds, cost_usd, culprit, ...}}."""
    from pathlib import Path

    from .bisect import run_investigation

    out: dict[str, Any] = {}
    for name in ("plain", "smart"):
        store = StateStore(Path(base_dir) / f"compare-{name}")
        cp = store.create(st.repo, spec=st.spec, good_sha=st.good_sha, bad_sha=st.bad_sha)
        strat = strategy_factory(name, store)
        o = run_investigation(store, cp, runner_factory(store, cp), strat)
        m = cp.metrics
        out[name] = {"outcome": o.kind, "culprit": o.sha, "eval_rounds": m.get("eval_rounds", 0),
                     "cost_usd": m.get("cost_usd", 0.0), "steps": cp.step, "commits_evaluated": m.get("commits_evaluated", 0),
                     "fell_back_to_plain": (m.get("planner") or {}).get("fell_back_to_plain", False),
                     "context_tokens": (m.get("planner") or {}).get("context_tokens")}
    p, s = out["plain"], out["smart"]
    out["saved_rounds"] = p["eval_rounds"] - s["eval_rounds"]
    out["same_culprit"] = p["culprit"] == s["culprit"]
    return out


def format_comparison(c: dict[str, Any]) -> str:
    lines = ["strategy  rounds  cost_usd  steps  culprit"]
    for k in ("plain", "smart"):
        r = c[k]
        lines.append(f"{k:<9} {r['eval_rounds']:>6}  {r['cost_usd']:>8}  {r['steps']:>5}  {(r['culprit'] or r['outcome'])[:8]}")
    lines.append(f"smart saved {c['saved_rounds']} eval rounds; same culprit: {c['same_culprit']}")
    ct = c["smart"].get("context_tokens")
    if ct:
        lines.append(f"planner context tokens: {ct['with_graph']} with graph vs {ct['without_graph']} reading the raw range")
    return "\n".join(lines)
