"""`agentbisect` entrypoint: investigate (intake + plain bisect), resume, status; watch is a later chunk."""
from __future__ import annotations

import argparse
import json
import sys

from .intake import (FixtureSource, IntakeError, IntakeResult, NeatlogsCliSource, WaitingOnHumanLogin,
                     intake_detection, intake_issue, intake_trace)
from .models import BisectState
from .state import DEFAULT_DIR, StateError, StateStore
from .verify import describe_revert
from .tracing import init_tracing, shutdown_tracing


def _summary(st: BisectState) -> str:
    verdicts = {s: r.verdict for s, r in st.results.items()}
    tokens = sum(t["tokens"] for t in st.context_tokens_per_step)
    lines = [f"investigation {st.id}  status={st.status}  step={st.step}  repo={st.repo}",
             f"  range: good={st.good_sha or '?'} bad={st.bad_sha or '?'}  mode={st.plan.mode if st.plan else '-'}",
             f"  commits tested: {len(verdicts)}  " + " ".join(f"{s[:8]}={v}" for s, v in verdicts.items()),
             f"  context tokens logged: {tokens} over {len(st.context_tokens_per_step)} entries"]
    if st.in_flight:
        lines.append(f"  INTERRUPTED mid-step: {st.in_flight}")
    if st.error:
        lines.append(f"  error: {st.error}")
    lines += describe_revert(st)
    return "\n".join(lines)


def _add_intake_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--trace", help="neatlogs trace id of a bad run")
    g.add_argument("--detection", help="neatlogs detection id")
    g.add_argument("--issue", help="GitHub issue URL (read with `gh issue view`)")
    p.add_argument("--repo", required=True, help="path to the target repo (has evals/cases.jsonl)")
    p.add_argument("--fixtures", help="recorded JSON trace fixtures instead of the live neatlogs CLI")
    p.add_argument("--project", help="neatlogs project uuid (default $NEATLOGS_PROJECT_ID)")
    p.add_argument("--workflow", default="support-agent", help="neatlogs workflow name of the target's runs")
    p.add_argument("--strategy", choices=["smart", "plain"], default="smart",
                   help="bisect strategy run after intake (default smart: rank commits by trace divergence, "
                        "code impact and checkpoint intent, then fall back to plain)")
    p.add_argument("--intake-only", action="store_true", help="stop after intake; do not run the bisect")
    p.add_argument("--good", help="known-good commit (overrides the one intake found)")
    p.add_argument("--bad", help="known-bad commit (default: failing trace's commit, else HEAD)")


def trace_source(a: argparse.Namespace | None = None, meta: dict | None = None):
    """Trace source for span divergence; `meta` ({fixtures, project}) comes from a saved investigation."""
    fixtures = getattr(a, "fixtures", None) if a else (meta or {}).get("fixtures")
    project = getattr(a, "project", None) if a else (meta or {}).get("project")
    return FixtureSource(fixtures) if fixtures else NeatlogsCliSource(project=project)


def make_strategy(st: BisectState, store: StateStore, meta: dict | None = None):
    """Strategy for an investigation: None means the engine default (plain)."""
    if st.metrics.get("strategy", "plain") != "smart":
        return None
    from .planner import Planner, SmartStrategy
    try:
        source = trace_source(meta=meta)
    except Exception:  # no usable trace source: the planner ranks by checkpoint intent alone
        source = None
    return SmartStrategy(Planner(st.repo, source=source), store)


def run_intake(a: argparse.Namespace) -> IntakeResult:
    if a.issue:
        return intake_issue(a.issue, a.repo)
    src = FixtureSource(a.fixtures) if a.fixtures else NeatlogsCliSource(project=a.project)
    if a.trace:
        return intake_trace(src, a.trace, a.repo, workflow=a.workflow)
    return intake_detection(src, a.detection, a.repo, workflow=a.workflow)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentbisect", description="git bisect for agent behavior")
    p.add_argument("--state-dir", default=DEFAULT_DIR, help=f"state directory (default {DEFAULT_DIR})")
    sub = p.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("investigate", help="run intake and record the RegressionSpec in a new investigation")
    _add_intake_args(inv)
    _add_intake_args(sub.add_parser("intake", help="run intake and print the RegressionSpec (no state written)"))
    c = sub.add_parser("compare", help="run plain and smart bisect on one investigation (separate empty caches) "
                                       "and print eval rounds and cost")
    c.add_argument("id")
    c.add_argument("--fixtures", help="recorded trace fixtures for the smart planner's span divergence")
    c.add_argument("--project", help="neatlogs project uuid")
    sub.add_parser("watch", help="poll neatlogs detections and auto-investigate (not implemented yet)")
    r = sub.add_parser("resume", help="resume an interrupted investigation")
    r.add_argument("id")
    s = sub.add_parser("status", help="show investigations")
    s.add_argument("id", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    store = StateStore(a.state_dir)
    if a.cmd in ("investigate", "intake"):
        try:
            res = run_intake(a)
        except WaitingOnHumanLogin as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        except IntakeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if a.cmd == "intake":
            print(json.dumps(res.to_dict(), indent=2))
            return 0
        st = store.create(a.repo, spec=res.spec, good_sha=res.good_sha, bad_sha=res.bad_sha)
        print(f"investigation {st.id} created (status={st.status}); spec: {len(res.spec.cases)} cases, "
              f"{len(res.spec.held_out)} held out, {len(res.spec.good_trace_ids)} good traces, "
              f"range good={res.good_sha or '?'} bad={res.bad_sha or '?'}")
        for n in res.notes:
            print(f"  - {n}")
        if a.good:
            st.good_sha = a.good
        if a.bad:
            st.bad_sha = a.bad
        store.save(st)
        meta = {"fixtures": a.fixtures, "project": a.project}
        st.metrics["strategy"] = a.strategy
        st.metrics["source"] = meta
        store.save(st)
        if a.intake_only:
            print("intake only; run `agentbisect resume " + st.id + "` to bisect")
            return 0
        from .bisect import resume_investigation
        init_tracing(st.id)
        try:
            return _explain(store, st, int(resume_investigation(store, st, strategy=make_strategy(st, store, meta))),
                            meta)
        finally:
            shutdown_tracing()
    if a.cmd == "watch":
        print("agentbisect watch: not implemented yet (later chunk)", file=sys.stderr)
        return 2
    try:
        if a.cmd == "compare":
            return _compare(a, store)
        if a.cmd == "status":
            states = [store.load(a.id)] if a.id else store.list()
            if not states:
                print("no investigations")
            for st in states:
                print(_summary(st))
            return 0
        st = store.load(a.id)
        redo = store.resume(st)
        init_tracing(st.id)
        try:
            print(f"resumed {st.id}" + (f"; will redo interrupted step: {redo}" if redo else ""))
            from .bisect import resume_investigation
            meta = st.metrics.get("source")
            return _explain(store, st, int(resume_investigation(store, st, strategy=make_strategy(
                st, store, meta)) or 0), meta)
        finally:
            shutdown_tracing()
    except StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _explain(store: StateStore, st: BisectState, code: int, meta: dict | None) -> int:
    """After a bisect that found a culprit: culprit analysis, then the revert test (both resume-safe)."""
    if code != 0 or st.status != "culprit_found" or not st.culprit:
        return code
    from .verify import confirm_culprit
    try:
        source = trace_source(meta=meta)
    except Exception:
        source = None
    try:
        confirm_culprit(store, st, source=source)
    except Exception as e:  # keep the culprit; `resume` redoes the interrupted step
        print(f"error during culprit analysis / revert test: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("\n".join(describe_revert(st)))
    return 0


def _compare(a: argparse.Namespace, store: StateStore) -> int:
    from .bisect import make_runner
    from .planner import Planner, SmartStrategy, compare_strategies, format_comparison
    st = store.load(a.id)
    meta = {"fixtures": a.fixtures, "project": a.project}

    def strategies(name: str, s: StateStore):
        if name == "plain":
            return None
        return SmartStrategy(Planner(st.repo, source=trace_source(meta=meta)), s)

    res = compare_strategies(st, str(store.base), make_runner, strategies)
    print(format_comparison(res))
    return 0
