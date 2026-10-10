"""Culprit analyst (Explain stage): why was the culprit commit written, what did it change, where did behaviour diverge.

Inputs are all read-only: the commit's Entire checkpoint (`entire checkpoint explain <sha> --full`), the
entire-graph semantic diff (`entire graph diff`) and blast radius (`entire graph impact`), and the good/bad traces.
Each degrades gracefully: no checkpoint -> commit message; no graph -> `git diff --stat` + hunk-header symbols;
no trace source -> divergence is reported as unavailable.

The original intent is what the Fixer must preserve, so it is kept as its own labelled text. `fixer_brief` builds the
only context the Fixer receives: the culprit diff, that intent and the failing evidence, never other history.
"""
from __future__ import annotations

import json
import re

from .intake import TraceSource
from .models import BisectState, CulpritReport
from .planner import (GRAPH_UNAVAILABLE, EntireGraph, Git, GraphAdapter, GraphUnavailable, Run, _is_test_path,
                      _run, first_divergence)

INTENT_LABEL = "ORIGINAL INTENT (the Fixer must preserve this)"
MAX_REASONING_CHARS = 3000
MAX_DIFF_CHARS = 20000
MAX_TRACE_LINES = 30
IMPACT_LOOKUPS = 4
NO_CHECKPOINT_MARKERS = ("no Entire-Checkpoint trailer", "checkpoint not found")


# ---------------------------------------------------------------- checkpoint intent

def parse_intent_parts(text: str) -> tuple[str, str] | None:
    """(prompt, reasoning) from `entire checkpoint explain <sha> --full`, or None when there is no checkpoint.
    Verified shape: header `● Checkpoint <id>`, `## Intent` (truncated with '...'), `## Summary`, `## Files`, then
    `── Transcript` whose first `[User]` block holds the coding agent's `Request:` and `[Assistant]` lines narrate
    the reasoning. Without a transcript the (truncated) `## Intent` header is the best available prompt."""
    if any(m in text for m in NO_CHECKPOINT_MARKERS) or "● Checkpoint" not in text:
        return None
    head, _, transcript = text.partition("── Transcript")
    prompt = ""
    user = re.search(r"\[User\](.*?)(?=\n\[(?:Tool|Assistant|User)\]|\Z)", transcript, re.S)
    if user:
        body = user.group(1).strip()
        m = re.search(r"Request:\s*(.*)", body, re.S)
        prompt = (m.group(1) if m else body).strip()
    if not prompt:
        im = re.search(r"## Intent\s*\n+(.*?)(?=\n## |\n──|\Z)", head, re.S)
        prompt = im.group(1).strip() if im else ""
    reasoning: list[str] = []
    sm = re.search(r"## Summary\s*\n+(.*?)(?=\n## |\n──|\Z)", head, re.S)
    if sm and "Not generated yet" not in sm.group(1):
        reasoning.append(sm.group(1).strip())
    reasoning += [a.strip() for a in re.findall(r"\[Assistant\]\s*(.*)", transcript) if a.strip()]
    return prompt, "\n".join(reasoning)[:MAX_REASONING_CHARS]


def format_intent(prompt: str, reasoning: str) -> str:
    out = f"Prompt: {prompt}" if prompt else ""
    if reasoning:
        out += ("\n\n" if out else "") + f"Reasoning:\n{reasoning}"
    return out


def read_intent(git: Git, run: Run, repo: str, sha: str, binary: str = "entire") -> tuple[str, str]:
    """-> (intent text, source) where source is 'checkpoint' or 'commit_message'."""
    rc, out, _ = run([binary, "checkpoint", "explain", sha, "--full"], repo)
    parts = parse_intent_parts(out) if rc == 0 else None
    if parts and (parts[0] or parts[1]):
        return format_intent(*parts), "checkpoint"
    return f"Commit message: {git.message(sha)}", "commit_message"


# ---------------------------------------------------------------- semantic diff + impact

_HUNK_SYMBOL = re.compile(r"^@@[^@]*@@\s*(?:async\s+)?(?:def|class|function|func|fn)\s+([A-Za-z_][\w]*)", re.M)


def _graph_diff(run: Run, repo: str, sha: str, binary: str) -> tuple[list[str], list[str]]:
    rc, out, err = run([binary, "graph", "diff", "--base", f"{sha}~1", "--head", sha, "--json", "--repo", "."], repo)
    if rc != 0:
        raise GraphUnavailable(f"{GRAPH_UNAVAILABLE}: entire graph diff rc={rc}: {(err or out).strip()[:200]}")
    try:
        d = json.loads(out)
    except ValueError as e:
        raise GraphUnavailable(f"{GRAPH_UNAVAILABLE}: non-JSON output from entire graph diff") from e
    lines: list[str] = []
    symbols: list[str] = []
    for f in d.get("files") or []:
        test = _is_test_path(f.get("path", ""))
        for c in f.get("changes") or []:
            dep = c.get("dependents_count")
            lines.append(f"{f.get('path')}: {c.get('type')} {c.get('kind')} {c.get('name')}"
                         + (f" ({dep} dependents)" if dep else ""))
            if not test and c.get("name"):
                symbols.append(c["name"])
    return lines, symbols


def semantic_diff(repo: str, sha: str, git: Git, run: Run, graph: GraphAdapter | None,
                  binary: str = "entire") -> tuple[str, list[str], list[str]]:
    """-> (semantic diff text, impacted symbols, notes). Graph first, git fallback."""
    notes: list[str] = []
    try:
        lines, symbols = _graph_diff(run, repo, sha, binary)
        if lines:
            try:  # blast radius: who calls the changed non-test entities
                g = graph if graph is not None else EntireGraph(repo, run, binary)
                for name in list(dict.fromkeys(symbols))[:IMPACT_LOOKUPS]:
                    symbols += g.impact(name.rsplit(".", 1)[-1]).symbols
            except GraphUnavailable as e:
                notes.append(f"impact unavailable: {e}")
            return "\n".join(lines), list(dict.fromkeys(symbols)), notes
        notes.append("entire graph diff returned no entity changes; using git diff --stat")
    except GraphUnavailable as e:
        notes.append(f"{e}; using git diff --stat")
    try:
        stat = git.out("show", "--format=", "--stat", sha).strip()
        hunks = git.out("show", "--format=", "-U0", sha)
    except RuntimeError as e:
        return "", [], notes + [f"git diff failed: {e}"]
    symbols = list(dict.fromkeys(_HUNK_SYMBOL.findall(hunks))) or git.files_of(sha)
    return stat, symbols, notes


# ---------------------------------------------------------------- trace divergence

def trace_divergence(st: BisectState, source: TraceSource | None) -> list[str]:
    """Span-by-span summary of the first good trace against the bad one."""
    spec = st.spec
    if not source or not spec or not spec.bad_trace_id or not spec.good_trace_ids:
        return ["trace divergence unavailable: no trace source or no good/bad trace ids"]
    try:
        bad = source.spans(spec.bad_trace_id)
        good: list = []
        for gid in spec.good_trace_ids:
            good = source.spans(gid)
            if good:
                break
    except Exception as e:  # trace access must never kill the investigation (login prompt etc.)
        return [f"trace divergence unavailable: {type(e).__name__}: {e}"]
    if not good or not bad:
        return ["trace divergence unavailable: empty trace"]
    div = first_divergence(good, bad)
    out = [f"first divergence: {div.describe()}" if div else "no divergence: the bad trace takes the same steps"]
    gs, bs = ([s for s in x if s.tools] or x for x in (good, bad))
    for i in range(min(max(len(gs), len(bs)), MAX_TRACE_LINES)):
        g = gs[i].signature if i < len(gs) else "(none)"
        b = bs[i].signature if i < len(bs) else "(none)"
        out.append(f"step {i}: good={g} | bad={b}" + ("" if g == b else "   <-- differs"))
    return out


# ---------------------------------------------------------------- failing evidence

def failing_evidence(st: BisectState, sha: str) -> list[str]:
    """What the Fixer may be told about the failure: cases, rates and the bad trace, never other commits."""
    ev: list[str] = []
    if st.spec and st.spec.bad_trace_id:
        ev.append(f"failing trace: {st.spec.bad_trace_id}")
    bad = st.results.get(sha)
    if bad:
        if bad.pass_rate is not None:
            ev.append(f"pass rate with the culprit: {bad.pass_rate:.2f} over {bad.executed} runs")
        if bad.failing_cases:
            ev.append("failing cases: " + ", ".join(bad.failing_cases))
        ev += [f"failing trace id: {t}" for t in bad.trace_ids[:3]]
    good = st.results.get(st.good_sha or "")
    if good and good.pass_rate is not None:
        ev.append(f"pass rate before the culprit: {good.pass_rate:.2f}")
    if st.plan and st.plan.divergent_span:
        ev.append(f"divergence: {st.plan.divergent_span}")
    if st.spec and st.spec.predicate:
        ev.append(f"pass/fail rule: {json.dumps(st.spec.predicate, sort_keys=True)}")
    return ev


# ---------------------------------------------------------------- the analyst

def analyze_culprit(st: BisectState, sha: str, *, source: TraceSource | None = None, run: Run = _run,
                    git: Git | None = None, graph: GraphAdapter | None = None, binary: str = "entire") -> CulpritReport:
    """Build the CulpritReport for a confirmed culprit. Keeps bisect's confidence; revert test fills the rest."""
    git = git or Git(st.repo, run)
    prior = st.culprit if st.culprit and st.culprit.sha == sha else None
    intent, src = read_intent(git, run, st.repo, sha, binary)
    sem, symbols, notes = semantic_diff(st.repo, sha, git, run, graph, binary)
    rep = CulpritReport(
        sha=sha, checkpoint_intent=intent, semantic_diff=sem, impacted_symbols=symbols,
        confidence=prior.confidence if prior else "", intent_source=src,
        trace_divergence=trace_divergence(st, source), failing_evidence=failing_evidence(st, sha),
        revert_test_passed=prior.revert_test_passed if prior else None,
        flags=list(prior.flags) if prior else [], revert_details=dict(prior.revert_details) if prior else {},
        analyzed=True)
    if src == "commit_message":
        rep.flags.append("no checkpoint: intent taken from the commit message")
    rep.revert_details.setdefault("analysis_notes", []).extend(notes)
    return rep


def fixer_brief(repo: str, report: CulpritReport, run: Run = _run) -> str:
    """The Fixer's entire context: culprit diff + original intent + failing evidence. No log, no other commits."""
    rc, patch, err = run(["git", "-C", repo, "show", "--format=", "--patch", report.sha], repo)
    if rc != 0:
        raise RuntimeError(f"git show {report.sha} failed: {err.strip()}")
    if len(patch) > MAX_DIFF_CHARS:
        patch = patch[:MAX_DIFF_CHARS] + "\n... (diff truncated)\n"
    evidence = "\n".join(f"- {e}" for e in report.failing_evidence) or "- (none recorded)"
    return (f"## Culprit diff (this change introduced the regression)\n\n```diff\n{patch.rstrip()}\n```\n\n"
            f"## {INTENT_LABEL}\n\n{report.checkpoint_intent or '(unknown)'}\n\n"
            f"## Failing evidence\n\n{evidence}\n")
