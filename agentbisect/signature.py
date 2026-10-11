"""Failure signatures: the specific failing check behind a bad run, so the bisect predicate can be "does THIS
signature reproduce?" instead of "does the case fail?".

A harness result (support-agent-demo `evals` schema_version 1) lists `checks: [{check, passed, reason}]`, a `budget`
block and an optional `judge` block. Each failed one becomes a failure record {key, check, detail, expected, got,
reason}; `key` is the identity used to match runs and commits:

  call_args:<tool>.<arg>   a tool call whose argument is wrong (expected/got parsed from the check reason)
  budget:steps|tokens      step or token budget exceeded
  judge                    rubric failed
  <check name>             any other deterministic check (reply_regex, must_call, ...)

Other failures (e.g. a budget regression elsewhere) are simply different keys and count as a pass for this one.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from typing import Any, Iterable

from .models import BisectState, CommitResult, FailureSignature
from .state import StateStore

_CALL_ARGS = re.compile(r"no (\w+) call matching (\{.*?\}) \(saw (\[.*?\])\)(?:;|$)", re.S)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _same(a: Any, b: Any) -> bool:
    x, y = _num(a), _num(b)
    return abs(x - y) <= 0.005 if x is not None and y is not None else a == b


def _call_args_failures(reason: str) -> list[dict[str, str]]:
    out = []
    for tool, want_s, saw_s in _CALL_ARGS.findall(reason):
        try:
            want, saw = ast.literal_eval(want_s), ast.literal_eval(saw_s)
        except (ValueError, SyntaxError):
            out.append({"key": f"call_args:{tool}", "detail": tool, "expected": "", "got": ""})
            continue
        if not saw:
            out.append({"key": f"call_args:{tool}", "detail": tool, "expected": "a call", "got": "no call"})
            continue
        bad = [k for k, v in want.items() if not any(_same(c.get(k), v) for c in saw)]
        for k in bad or list(want):
            out.append({"key": f"call_args:{tool}.{k}", "detail": f"{tool}.{k}", "expected": str(want[k]),
                        "got": str(saw[0].get(k))})
    return out


def failure_records(check: str, reason: str) -> list[dict[str, str]]:
    base = {"check": check, "reason": reason[:300]}
    if check == "call_args":
        recs = _call_args_failures(reason)
        if recs:
            return [{**base, **r} for r in recs]
    return [{**base, "key": check, "detail": "", "expected": "", "got": ""}]


def failures_of_result(r: dict[str, Any]) -> list[dict[str, str]]:
    """Failure records for one harness result dict (empty for passing runs). A failing run with no recorded
    cause still gets a generic `case_fail` record so it is never silently treated as a pass."""
    out: list[dict[str, str]] = []
    for c in r.get("checks") or []:
        if not c.get("passed", True):
            out += failure_records(str(c.get("check")), str(c.get("reason") or ""))
    b = r.get("budget") or {}
    if b.get("status") == "fail":
        reason = str(b.get("reason") or "")
        kinds = [k for k in ("steps", "tokens") if k in reason] or ["steps"]
        out += [{"key": f"budget:{k}", "check": "budget", "detail": k, "expected": "", "got": "",
                 "reason": reason[:300]} for k in kinds]
    j = r.get("judge")
    if isinstance(j, dict) and j.get("passed") is False:
        out.append({"key": "judge", "check": "judge", "detail": "", "expected": "", "got": "",
                    "reason": str(j.get("reason") or "")[:300]})
    if not out and r.get("status") == "fail":
        out.append({"key": "case_fail", "check": "case_fail", "detail": "", "expected": "", "got": "",
                    "reason": str(r.get("reason") or "")[:300]})
    return out


def signature_from_record(rec: dict[str, str]) -> FailureSignature:
    return FailureSignature(key=rec["key"], check=rec.get("check", ""), detail=rec.get("detail", ""),
                            expected=rec.get("expected", ""), got=rec.get("got", ""), reason=rec.get("reason", ""))


def signature_from_key(key: str) -> FailureSignature:
    """For `--signature KEY` on the command line."""
    check, _, detail = key.partition(":")
    return FailureSignature(key=key, check=check, detail=detail)


def signatures_of(result: CommitResult) -> list[FailureSignature]:
    """All failing checks seen at a commit, one signature each, most frequent first."""
    sigs = [FailureSignature.from_dict(d) for d in result.failure_signatures]
    return sorted(sigs, key=lambda s: (-s.fails, s.key))


def group_signatures(infos: dict[str, dict[str, Any]], case_runs: dict[str, int],
                     case_fails: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """infos: key -> first-seen record; case_fails: case -> {key: failing runs}; case_runs: case -> executed runs.
    -> signature dicts with the cases where the key fires and run counts over those cases."""
    by_key: dict[str, list[str]] = defaultdict(list)
    for case, ks in case_fails.items():
        for k, n in ks.items():
            if n > 0:
                by_key[k].append(case)
    out = []
    for k, cases in by_key.items():
        cases = sorted(cases)
        s = signature_from_record(infos.get(k) or {"key": k})
        s.cases = cases
        s.fails = sum(case_fails[c][k] for c in cases)
        s.runs = sum(case_runs.get(c, 0) for c in cases)
        out.append(s.to_dict())
    return out


def pick_primary(sigs: Iterable[FailureSignature], bad_case: str | None) -> FailureSignature | None:
    """The signature to bisect first: one that fires in the bad trace's case, else any. A wrong behaviour (a
    deterministic check) outranks a cost miss (budget), since the report of a bad trace is about wrong output;
    then the most frequent. The others are bisected as siblings."""
    sigs = sorted(sigs, key=lambda s: (s.check == "budget", s.check == "judge", -s.fails, s.key))
    if bad_case:
        mine = [s for s in sigs if bad_case in s.cases]
        if mine:
            return mine[0]
    return sigs[0] if sigs else None


def signature_from_trace(metadata: dict[str, Any], tags: list[str]) -> FailureSignature | None:
    """A trace that carries its own failing check: metadata `failed_checks` ([{check, reason}]) or a
    `check:<key>` tag. Real neatlogs traces do not carry this today; the bad end's eval result is used then."""
    for fc in metadata.get("failed_checks") or []:
        if isinstance(fc, dict) and fc.get("check"):
            return signature_from_record(failure_records(str(fc["check"]), str(fc.get("reason") or ""))[0])
    for t in tags:
        if t.startswith("check:") and len(t) > 6:
            return signature_from_key(t[6:])
    return None


# -- planner feed --

def symptom_text(sig: FailureSignature) -> str:
    """Words for the planner's intent scoring."""
    if sig.check == "case_fail":  # no specific check known: nothing to add to the symptom
        return ""
    bits = [sig.check, sig.detail.replace(".", " ").replace("_", " "), sig.reason]
    if sig.expected or sig.got:
        bits += [f"expected {sig.expected}", f"got {sig.got}"]
    e, g = _try_float(sig.expected), _try_float(sig.got)
    if e and g and (abs(g / e - 100) < 1e-6 or abs(g / e - 0.01) < 1e-9):  # 64.0 vs 6400: a units mix-up
        bits.append("cents dollars unit conversion")
    return " ".join(b for b in bits if b)


def _try_float(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def symbol_hints(sig: FailureSignature) -> list[str]:
    """Names to look up with `entire graph impact`: the tool behind a call_args failure, plus refund/amount
    vocabulary when the failing value is a money amount."""
    hints: list[str] = []
    if sig.check == "call_args" and sig.detail:
        tool = sig.detail.split(".")[0]
        hints += [tool, f"_tool_{tool}"]
    text = f"{sig.detail} {sig.reason}".lower()
    hints += [w for w in ("refund", "amount") if w in text]
    return list(dict.fromkeys(hints))


# -- overlapping regressions: one investigation per signature --

def spawn_siblings(store: StateStore, st: BisectState) -> list[BisectState]:
    """After the primary investigation discovered several failing checks at the bad end, create one more
    investigation per remaining signature (same repo, range, strategy; shared verdict cache, so commits already
    evaluated for the cases they share cost nothing). Idempotent: siblings are recorded on the primary."""
    from .models import RegressionSpec
    found = [FailureSignature.from_dict(d) for d in st.metrics.get("signatures", [])]
    mine = st.spec.signature.key if st.spec and st.spec.signature else None
    done = {x["key"]: x["id"] for x in st.metrics.get("signature_siblings", [])}
    made = []
    for sig in found:
        if sig.key == mine or sig.key in done:
            continue
        spec = RegressionSpec.from_dict({**st.spec.to_dict(), "signature": sig.to_dict()})
        sib = store.create(st.repo, spec=spec, good_sha=st.good_sha, bad_sha=st.bad_sha)
        sib.metrics.update(strategy=st.metrics.get("strategy", "plain"), source=st.metrics.get("source"),
                           signature_group=st.id, signatures_discovered=True)
        store.save(sib)
        st.metrics.setdefault("signature_siblings", []).append({"id": sib.id, "key": sig.key})
        made.append(sib)
    store.save(st)
    return made
