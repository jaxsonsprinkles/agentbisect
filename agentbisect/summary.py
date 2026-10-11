"""One row per failure signature: culprit, revert test before/after, smart vs plain rounds. Built only from saved
state (nothing is hardcoded or compared against expected answers), so the table of a final re-run reads the same
way as any other run. Used by `agentbisect summary` and by the per-signature section of the reports."""
from __future__ import annotations

import json
from typing import Any

from .bisect import KNOWN_PREEXISTING
from .models import BisectState
from .state import StateError, StateStore

HEAD = ["signature", "outcome", "revert test (before -> after)", "smart vs plain (eval rounds)"]


def _num(v: Any) -> str:
    return "n/a" if v is None else f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 100 else f"{v:g}"


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def revert_cell(st: BisectState) -> str:
    c, rt = st.culprit, st.metrics.get("revert_test") or {}
    if not c or "revert_test" not in st.metrics:
        return "not run"
    verdict = {True: "PASSED", False: "FAILED", None: "inconclusive"}[c.revert_test_passed]
    b = rt.get("budget")
    if b:
        bef, aft = b.get("before") or {}, b.get("after") or {}
        kind = aft.get("kind") or bef.get("kind") or "metric"
        return (f"{verdict}: median {kind} {_num(bef.get('median'))} -> {_num(aft.get('median'))}, "
                f"within budget {bef.get('within')}/{bef.get('total')} -> {aft.get('within')}/{aft.get('total')}")
    if rt.get("reverted_pass_rate") is None:
        return verdict
    return f"{verdict}: pass {_pct(rt.get('culprit_pass_rate'))} -> {_pct(rt['reverted_pass_rate'])}"


def rounds_cell(st: BisectState) -> str:
    cmp_ = st.metrics.get("comparison")
    if not cmp_ or "plain" not in cmp_ or "smart" not in cmp_:
        return "n/a (no compare data)"
    p, s = cmp_["plain"], cmp_["smart"]
    same = "" if cmp_.get("same_culprit") in (None, True) else ", DIFFERENT culprit"
    return f"{p.get('eval_rounds')} plain vs {s.get('eval_rounds')} smart{same}"


def outcome_cell(st: BisectState) -> str:
    if st.status == "refused" or st.metrics.get("known_preexisting"):
        return KNOWN_PREEXISTING
    if st.culprit:
        return f"culprit {st.culprit.sha[:10]} (confidence {st.culprit.confidence or '?'})"
    if st.status == "failed":
        return f"failed: {(st.error or 'unknown error')[:120]}"
    return st.status


def summary_rows(states: list[BisectState]) -> list[dict[str, Any]]:
    out = []
    for st in states:  # caller's order: store.list() is by creation, group_states() is primary first
        sig = st.spec.signature if st.spec else None
        out.append({"id": st.id, "signature": sig.key if sig else "(whole-case pass rate)", "status": st.status,
                    "outcome": outcome_cell(st), "culprit": st.culprit.sha if st.culprit else None,
                    "revert": revert_cell(st), "rounds": rounds_cell(st)})
    return out


def cells(r: dict[str, Any]) -> list[str]:
    return [r["signature"], r["outcome"], r["revert"], r["rounds"]]


def render_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no investigations"
    table = [HEAD, *map(cells, rows)]
    w = [max(len(row[i]) for row in table) for i in range(len(HEAD))]
    fmt = lambda row: "  ".join(c.ljust(w[i]) for i, c in enumerate(row)).rstrip()  # noqa: E731
    return "\n".join([fmt(table[0]), fmt(["-" * x for x in w]), *map(fmt, table[1:])])


def render_markdown(rows: list[dict[str, Any]]) -> str:
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = ["| " + " | ".join(HEAD) + " |", "|" + "---|" * len(HEAD)]
    lines += ["| " + " | ".join(esc(c) for c in cells(r)) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def render(rows: list[dict[str, Any]], fmt: str = "text") -> str:
    return {"text": render_text, "md": render_markdown, "json": lambda r: json.dumps(r, indent=2)}[fmt](rows)


def group_states(store: StateStore, st: BisectState) -> list[BisectState]:
    """The investigation plus its sibling signatures (primary and siblings of one `investigate` call)."""
    try:
        root = store.load(st.metrics["signature_group"]) if st.metrics.get("signature_group") else st
    except StateError:
        root = st
    ids = [root.id, *[x["id"] for x in root.metrics.get("signature_siblings", [])]]
    out = []
    for i in dict.fromkeys(ids):
        try:
            out.append(st if i == st.id else store.load(i))
        except StateError:
            pass
    return out or [st]
