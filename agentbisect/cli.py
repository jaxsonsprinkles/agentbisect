"""`agentbisect` entrypoint. status/resume work on state files; investigate/watch are stubs for later chunks."""
from __future__ import annotations

import argparse
import sys

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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentbisect", description="git bisect for agent behavior")
    p.add_argument("--state-dir", default=DEFAULT_DIR, help=f"state directory (default {DEFAULT_DIR})")
    sub = p.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("investigate", help="start an investigation (not implemented yet)")
    inv.add_argument("--repo")
    inv.add_argument("--trace")
    inv.add_argument("--issue")
    sub.add_parser("watch", help="poll neatlogs detections and auto-investigate (not implemented yet)")
    r = sub.add_parser("resume", help="resume an interrupted investigation")
    r.add_argument("id")
    s = sub.add_parser("status", help="show investigations")
    s.add_argument("id", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    store = StateStore(a.state_dir)
    if a.cmd in ("investigate", "watch"):
        print(f"agentbisect {a.cmd}: not implemented yet (later chunk)", file=sys.stderr)
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
