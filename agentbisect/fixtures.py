"""Recorded good/bad trace pair, used when neatlogs span data is unavailable.

neatlogs multi-span traces currently finalize as `dlq` with 0 spans (evidence/neatlogs-dlq-report.md), so the
divergence step cannot read live spans. This module replays a pair recorded from real eval-runner output instead.
It is a FIXTURE, not live neatlogs: every trace carries a `fixture:recorded` tag, the manifest has a `label`, and
`describe()` prints that label first so a demo never passes it off as live data.

Fixture dir layout (one directory per recorded case):
    manifest.json   {"fixture": true, "label", "case", "good": {"file", "trace_id", ...}, "bad": {...}, ...}
    good.json       {"trace_id", "commit", "status", "input", "spans": [span dicts]}
    bad.json        same shape, from the commit that regressed

`FixtureDirSource` is a `TraceSource` (it extends `FixtureSource`), so anything that takes a trace source, such as
`culprit.trace_divergence` or the smart planner, works unchanged. `python -m agentbisect.fixtures DIR` prints the
first divergence; `--bundle OUT.json` writes the single-file form (`.bundle`) that `--fixtures` accepts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .intake import FixtureSource, IntakeError
from .planner import Divergence, first_divergence

FIXTURE_TAG = "fixture:recorded"
DEFAULT_WORKFLOW = "support-agent"
RESULT_CHARS = 300
DEFAULT_LABEL = ("FIXTURE: recorded from real eval-runner output (tool calls of one case at a good and a bad "
                 "commit). Not live neatlogs data.")


class FixtureError(IntakeError):
    """The fixture dir is missing files or malformed."""


def _read(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise FixtureError(f"cannot read fixture file {path}: {e}") from e
    if not isinstance(d, dict):
        raise FixtureError(f"{path} must contain a JSON object")
    return d


class FixtureDirSource(FixtureSource):
    """A `TraceSource` over a recorded good/bad pair in `path` (see module docstring for the layout)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.manifest = _read(self.path / "manifest.json")
        if not self.manifest.get("fixture"):
            raise FixtureError(f"{self.path}/manifest.json is not marked `fixture: true`; refusing to treat it as a "
                               "recorded fixture")
        self.label = str(self.manifest.get("label") or DEFAULT_LABEL)
        traces: list[dict] = []
        spans: dict[str, list] = {}
        self.ids: dict[str, str] = {}
        for role in ("good", "bad"):
            entry = self.manifest.get(role) or {}
            if not entry.get("file"):
                raise FixtureError(f"manifest has no `{role}.file`")
            d = _read(self.path / entry["file"])
            tid = str(d.get("trace_id") or entry.get("trace_id") or "")
            if not tid or not d.get("spans"):
                raise FixtureError(f"{entry['file']}: needs a trace_id and a non-empty spans list")
            self.ids[role] = tid
            spans[tid] = d["spans"]
            traces.append({"id": tid, "workflow": d.get("workflow", DEFAULT_WORKFLOW), "status": d.get("status", ""),
                           "started_at": d.get("started_at", ""), "input": d.get("input"),
                           "tags": list(d.get("tags") or []) + [FIXTURE_TAG],
                           "metadata": {"fixture": True, "role": role, "commit": d.get("commit")}})
        self.bundle = {"traces": traces, "spans": spans}
        super().__init__(self.bundle)

    @property
    def good_trace_id(self) -> str:
        return self.ids["good"]

    @property
    def bad_trace_id(self) -> str:
        return self.ids["bad"]

    def divergence(self) -> Divergence | None:
        """Where the bad run first differs from the good one (same function the planner and culprit use)."""
        return first_divergence(self.spans(self.good_trace_id), self.spans(self.bad_trace_id))

    def describe(self) -> str:
        div = self.divergence()
        return "\n".join([self.label,
                          f"good {self.good_trace_id} ({self.manifest['good'].get('commit', '?')}), "
                          f"bad {self.bad_trace_id} ({self.manifest['bad'].get('commit', '?')}), "
                          f"case {self.manifest.get('case', '?')}",
                          f"first divergence: {div.describe()}" if div else "no divergence: same steps"])


# ---------------------------------------------------------------- recording

def _span_dict(call: dict[str, Any]) -> dict[str, Any]:
    out = json.dumps(call.get("result"), sort_keys=True, default=str)
    return {"name": f"tool:{call['name']}", "kind": "tool", "tools": [call["name"]], "args": call.get("args") or {},
            "output": out if len(out) <= RESULT_CHARS else out[:RESULT_CHARS] + "..."}


def _result(report: dict[str, Any], case_id: str, repeat: int) -> dict[str, Any]:
    for r in report.get("results", []):
        if r.get("case_id") == case_id and r.get("repeat", 0) == repeat:
            return r
    raise FixtureError(f"no result for case {case_id!r} repeat {repeat} in report")


def record_pair(good_report: dict, bad_report: dict, case_id: str, out_dir: str | Path, *, case_input: str | None = None,
                repeat: int = 0, label: str = DEFAULT_LABEL, recorded_from: str = "") -> Path:
    """Write a fixture dir from two schema_version-1 eval reports (runner output) for one case.

    Only the tool-call spans the harness reports are kept (name, args, truncated result); model-call spans are not
    in the report, so they are absent from the fixture."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"fixture": True, "label": label, "case": case_id, "recorded_from": recorded_from,
                                "agent_model": bad_report.get("agent_model"), "judge_model": bad_report.get("judge_model")}
    for role, rep in (("good", good_report), ("bad", bad_report)):
        r = _result(rep, case_id, repeat)
        if not r.get("tool_calls"):
            raise FixtureError(f"{role} run of {case_id!r} made no tool calls; nothing to diverge on")
        trace = {"trace_id": r["trace_id"], "workflow": DEFAULT_WORKFLOW, "commit": rep.get("commit"),
                 "status": r["status"], "started_at": rep.get("started_at", ""), "input": case_input,
                 "tags": [f"commit:{rep.get('commit')}", f"case:{case_id}", f"run:{rep.get('run_id')}"],
                 "steps": r.get("steps"), "tokens": r.get("tokens"), "reply": r.get("reply"),
                 "spans": [_span_dict(c) for c in r.get("tool_calls", [])]}
        (out / f"{role}.json").write_text(json.dumps(trace, indent=2) + "\n")
        manifest[role] = {"file": f"{role}.json", "trace_id": r["trace_id"], "commit": rep.get("commit"),
                          "status": r["status"], "steps": r.get("steps"), "tokens": r.get("tokens")}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agentbisect.fixtures",
                                description="Show where a recorded bad run first diverges from the good one.")
    p.add_argument("dir", help="fixture directory (manifest.json, good.json, bad.json)")
    p.add_argument("--bundle", metavar="OUT.json", help="also write the single-file form accepted by --fixtures")
    a = p.parse_args(argv)
    try:
        src = FixtureDirSource(a.dir)
    except FixtureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(src.describe())
    if a.bundle:
        Path(a.bundle).write_text(json.dumps(src.bundle, indent=2) + "\n")
        print(f"wrote {a.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
