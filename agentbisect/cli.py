"""`agentbisect` entrypoint. status/resume work on state files; investigate/watch are stubs for later chunks."""
from __future__ import annotations

import argparse
import json
import sys

from .intake import (FixtureSource, IntakeError, IntakeResult, NeatlogsCliSource, WaitingOnHumanLogin,
                     intake_detection, intake_issue, intake_trace)
from .models import BisectState
from .state import DEFAULT_DIR, StateError, StateStore
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
    if st.culprit:
        lines.append(f"  culprit: {st.culprit.sha} (confidence {st.culprit.confidence or '?'})")
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
        print("bisect engine is a later chunk; run `agentbisect status " + st.id + "` to inspect")
        return 0
    if a.cmd == "watch":
        print("agentbisect watch: not implemented yet (later chunk)", file=sys.stderr)
        return 2
    try:
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
            try:
                from .bisect import resume_investigation  # type: ignore[import-not-found]  # chunk 6
            except ImportError:
                print("bisect engine not available yet; state is marked running and ready for it", file=sys.stderr)
                return 0
            return int(resume_investigation(store, st) or 0)
        finally:
            shutdown_tracing()
    except StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
