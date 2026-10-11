"""Reporter: Markdown + self-contained HTML report from a saved state file, and a gated `gh pr create`.

Both renderers work from one dict built by `build_report` so they always agree. Every string from the repo
(commit messages, intents, diffs) is untrusted: the HTML path escapes everything, the Markdown path puts it in
fences that cannot be closed from inside.

PR opening is DRY-RUN unless `confirm=True` (CLI: `--open-pr --confirm-open-pr`); only then do `git push` and
`gh pr create` run.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .models import BisectState, FixAttempt
from .verify import budget_line

PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
# Dashboard link pattern is UNVERIFIED (the docs only give the host https://app.neatlogs.com); override with
# AGENTBISECT_NEATLOGS_TRACE_URL. Trace ids are always printed next to any link.
DEFAULT_TRACE_URL = "https://app.neatlogs.com/traces/{trace_id}"
MULTI_REGRESSION = "multiple regressions likely"
SCORE_TOLERANCE = 0.1  # same slack the revert test uses against the good commit's pass rate
EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"


def trace_url(trace_id: str) -> str:
    tpl = os.environ.get("AGENTBISECT_NEATLOGS_TRACE_URL", DEFAULT_TRACE_URL)
    return tpl.replace("{project}", os.environ.get("NEATLOGS_PROJECT_ID", "")).replace("{trace_id}", trace_id)


def checkpoint_id(repo: str, ref: str) -> str | None:
    """Entire-Checkpoint trailer of a commit/branch, best effort (None when absent or git fails)."""
    try:
        out = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%(trailers:key=Entire-Checkpoint,valueonly)",
                              ref], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    v = out.stdout.strip().splitlines()
    return v[0].strip() if out.returncode == 0 and v and v[0].strip() else None


def _fmt(x: Any, pct: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.0f}%" if pct and isinstance(x, (int, float)) else str(x)


def fix_passes(a: FixAttempt, baseline: float | None) -> bool:
    ok_score = lambda s: s is not None and (baseline is None or s >= baseline - SCORE_TOLERANCE)  # noqa: E731
    return bool(a.intent_test_passed) and ok_score(a.eval_score) and ok_score(a.held_out_score)


def best_attempt(st: BisectState, baseline: float | None) -> FixAttempt | None:
    """First attempt that passes, else the closest one (highest held-out then eval score), else None."""
    if not st.fix_attempts:
        return None
    for a in st.fix_attempts:
        if fix_passes(a, baseline):
            return a
    return max(st.fix_attempts, key=lambda a: ((a.held_out_score or 0) + (a.eval_score or 0), a.attempt))


def build_report(st: BisectState, repo: str | None = None) -> dict[str, Any]:
    repo = repo or st.repo
    c, m = st.culprit, st.metrics
    rt = m.get("revert_test") or {}
    baseline = rt.get("baseline_pass_rate")
    if baseline is None and st.good_sha in st.results:
        baseline = st.results[st.good_sha].pass_rate
    before = rt.get("culprit_pass_rate")
    if before is None and c and c.sha in st.results:
        before = st.results[c.sha].pass_rate
    flags = list(dict.fromkeys([*(c.flags if c else []), *m.get("warnings", [])]))
    attempt = best_attempt(st, baseline)
    fix: dict[str, Any] | None = None
    if attempt:
        fix = {"branch": attempt.branch, "attempt": attempt.attempt, "attempts": len(st.fix_attempts),
               "before": before, "after": attempt.eval_score, "baseline": baseline,
               "held_out": attempt.held_out_score, "intent_test": attempt.intent_test_passed,
               "passed": fix_passes(attempt, baseline), "trace_ids": list(attempt.trace_ids), "diff": attempt.diff,
               "checkpoint": checkpoint_id(repo, attempt.branch) if repo else None}
    traces: list[tuple[str, str]] = []
    if st.spec and st.spec.bad_trace_id:
        traces.append(("failing trace", st.spec.bad_trace_id))
    traces += [("passing trace (earlier)", t) for t in (st.spec.good_trace_ids if st.spec else [])]
    if c:
        traces += [("eval run at culprit", t) for t in (st.results.get(c.sha).trace_ids if c.sha in st.results else [])]
    if fix:
        traces += [(f"fix attempt {fix['attempt']}", t) for t in fix["trace_ids"]]
    cmp_ = m.get("comparison")
    rounds = None
    if cmp_ and "plain" in cmp_ and "smart" in cmp_:
        rounds = {"plain": cmp_["plain"], "smart": cmp_["smart"], "saved": cmp_.get("saved_rounds")}
    elif m.get("eval_rounds") is not None:
        rounds = {m.get("strategy", "?"): {"eval_rounds": m.get("eval_rounds"), "cost_usd": m.get("cost_usd")}}
    sig = st.spec.signature if st.spec else None
    others = [d for d in m.get("signatures", []) if not sig or d.get("key") != sig.key]
    signature = {"label": sig.label, "key": sig.key, "cases": list(sig.cases), "others": others,
                 "siblings": list(m.get("signature_siblings", [])),
                 "group": m.get("signature_group")} if sig else None
    th = m.get("throttle") or {}
    throttle = (f"Model throttling: {th['backoffs']} backoff(s) totalling {th['waited_s']}s (exponential with jitter, "
                f"concurrency {th.get('concurrency')}); {th['recovered']} call(s) recovered, {th['gave_up']} gave up."
                ) if th.get("backoffs") else None
    return {
        "throttle": throttle, "signature": signature, "id": st.id, "repo": repo, "status": st.status, "good_sha": st.good_sha, "bad_sha": st.bad_sha,
        "culprit": c, "flags": flags, "multi": any(MULTI_REGRESSION in f for f in flags),
        "low_conf": bool(c and c.confidence == "low"),
        "revert": {"passed": c.revert_test_passed if c else None, "baseline": baseline, "culprit_rate": before,
                   "reverted": rt.get("reverted_pass_rate"), "ran": "revert_test" in m,
                   "budget": budget_line(rt["budget"]) if rt.get("budget") else None},
        "fix": fix, "rounds": rounds, "metrics": m,
        "steps": st.context_tokens_per_step,
        "commits": [{"sha": s, "verdict": r.verdict, "pass_rate": r.pass_rate, "repeats": r.repeats,
                     "runs": len(r.trace_ids), "cached": r.from_cache, "reason": r.reason}
                    for s, r in st.results.items()],
        "traces": traces,
        "culprit_checkpoint": checkpoint_id(repo, c.sha) if (c and repo) else None,
        "intent_labelled": c.intent_source == "checkpoint" if c else False,
    }


# ----------------------------------------------------------------------------------------------- Markdown

def _fence(text: str, lang: str = "") -> str:
    n = max([3] + [len(x) + 1 for x in re.findall(r"`+", text)])
    f = "`" * n
    return f"{f}{lang}\n{text.rstrip()}\n{f}"


def _md_cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _intent_label(r: dict[str, Any]) -> str:
    c = r["culprit"]
    if c and c.intent_source == "checkpoint":
        return "Original intent (from the Entire checkpoint of the commit; the coding agent's own prompt and reasoning)"
    return "Original intent (NO CHECKPOINT: reconstructed from the commit message only)"


def _signature_lines(r: dict[str, Any]) -> list[str]:
    sg = r.get("signature")
    if not sg:
        return []
    out = [f"Bisected on the failure signature `{sg['label']}` (key `{sg['key']}`; cases "
           f"{', '.join(sg['cases']) or 'all'}). A commit is bad only if THIS check fails; other failures count as "
           "a pass for this signature. AgentBisect separates overlapping regressions by failure signature."]
    if sg["others"]:
        out.append("Other failing checks at the bad end (separate signatures, each bisected on its own): "
                   + "; ".join(f"{d['key']} (cases {', '.join(d.get('cases', []))})" for d in sg["others"]))
    if sg["siblings"]:
        out.append("Sibling investigations: " + "; ".join(
            f"{x.get('id')} [{x.get('key')}] culprit {(x.get('culprit') or 'none')[:10]}" for x in sg["siblings"]))
    return out


def _signature_md(r: dict[str, Any]) -> list[str]:
    lines = _signature_lines(r)
    return ["## Failure signature", "", *[y for x in lines for y in (x, "")]] if lines else []


def render_markdown(r: dict[str, Any], pr: bool = False) -> str:
    c, fx = r["culprit"], r["fix"]
    L: list[str] = [f"# AgentBisect report: {c.sha[:10] if c else 'no culprit found'}", "",
                    f"Investigation `{r['id']}` - status `{r['status']}` - range `{(r['good_sha'] or '?')[:10]}`.."
                    f"`{(r['bad_sha'] or '?')[:10]}`", ""]
    if not c:
        L += ["No culprit was identified yet.", ""]
        return "\n".join(L) + "\n"
    L += [f"**Confidence: {c.confidence or 'unknown'}**", ""]
    if r["multi"] or r["low_conf"]:
        L += ["> **Warning: multiple regressions likely.** Verdicts conflict or the revert did not clear the "
              "regression, so this commit may be only one of several causes." if r["multi"] else
              "> **Warning: low confidence.** Treat this culprit as a lead, not a proof.", ""]
    other = [f for f in r["flags"] if MULTI_REGRESSION not in f]
    if other:
        L += ["Flags: " + "; ".join(other), ""]
    L += _signature_md(r)
    if r.get("throttle"):
        L += [r["throttle"], ""]
    L += ["## Culprit commit", "", f"`{c.sha}`", ""]
    if r["culprit_checkpoint"]:
        L += [f"Entire checkpoint: `{r['culprit_checkpoint']}`", ""]
    L += [f"## {_intent_label(r)}", "", _fence(c.checkpoint_intent or "(none recorded)", "text"), ""]
    L += ["## Semantic diff and impacted symbols", "", _fence(c.semantic_diff or "(none recorded)", "text"), ""]
    L += ["Impacted symbols: " + (", ".join(f"`{s}`" for s in c.impacted_symbols) or "none found"), ""]
    L += ["## Trace divergence (good vs bad, span by span)", ""]
    L += [f"- {d}" for d in c.trace_divergence] or ["No span divergence was available."]
    rv = r["revert"]
    res = {True: "PASSED: the regression disappears when the culprit is reverted on HEAD",
           False: "FAILED: the regression remains after reverting", None: "inconclusive" if rv["ran"] else "not run"}
    L += ["", "## Revert test", "", f"Result: **{res[rv['passed']]}**", "",
          f"Pass rate: good commit {_fmt(rv['baseline'])}, culprit {_fmt(rv['culprit_rate'])}, "
          f"HEAD with culprit reverted {_fmt(rv['reverted'])}.", ""]
    if rv["budget"]:
        L += [f"Budget (median over repeats vs the good-commit baseline): {rv['budget']}.", ""]
    if r["rounds"]:
        L += ["## Smart vs plain bisect", "", "| strategy | eval rounds | cost (USD, estimate) |", "|---|---|---|"]
        for k, v in r["rounds"].items():
            if isinstance(v, dict):
                L.append(f"| {k} | {_md_cell(v.get('eval_rounds'))} | {_md_cell(v.get('cost_usd'))} |")
        if r["rounds"].get("saved") is not None:
            L.append(f"\nSmart bisect saved {r['rounds']['saved']} eval rounds.")
        L.append("")
    L += ["## Fix", ""]
    if not fx:
        L += ["Fix: not attempted.", ""]
    else:
        verdict = "verified" if fx["passed"] else "NOT verified (draft PR: closest attempt shown)"
        L += [f"Branch `{fx['branch']}` - attempt {fx['attempt']} of {fx['attempts']} - **{verdict}**", "",
              "| metric | value |", "|---|---|",
              f"| pass rate before (culprit) | {_fmt(fx['before'])} |",
              f"| pass rate after (fix) | {_fmt(fx['after'])} |",
              f"| good-commit baseline | {_fmt(fx['baseline'])} |",
              f"| held-out score | {_fmt(fx['held_out'])} |",
              f"| intent test | {_fmt({True: 'passed', False: 'FAILED', None: 'not run'}[fx['intent_test']], False)} |", ""]
        if fx["checkpoint"]:
            L += [f"Fix checkpoint: `{fx['checkpoint']}`", ""]
        if fx["diff"]:
            L += [_fence(fx["diff"], "diff"), ""]
    if pr:
        return "\n".join(L) + f"\n{PR_FOOTER}\n"
    L += ["## Steps and context tokens", ""]
    if r["steps"]:
        L += ["| step | label | context tokens |", "|---|---|---|"]
        L += [f"| {_md_cell(s.get('step'))} | {_md_cell(s.get('label'))} | {_md_cell(s.get('tokens'))} |"
              for s in r["steps"]]
    else:
        L.append("No context-token entries were logged.")
    L += ["", "## Commits tested", ""]
    if r["commits"]:
        L += ["| commit | verdict | pass rate | repeats | runs |", "|---|---|---|---|---|"]
        L += [f"| `{x['sha'][:10]}` | {x['verdict']} | {_fmt(x['pass_rate'])} | {x['repeats']} | {x['runs']} |"
              for x in r["commits"]]
    L += ["", "## Links and evidence", "", "neatlogs traces (the dashboard URL pattern is a best guess; the id is "
          "always the source of truth):", ""]
    L += [f"- {_md_cell(lbl)}: `{t}` - <{trace_url(t)}>" for lbl, t in r["traces"]] or ["- none recorded"]
    L += ["", "Entire checkpoints (retrieve with the command, run in the target repo):", "",
          f"- culprit `{r['culprit_checkpoint'] or 'none found'}`: `entire checkpoint explain {c.sha} --full`"]
    if fx and fx["checkpoint"]:
        L.append(f"- fix `{fx['checkpoint']}`: `entire checkpoint explain {fx['branch']} --full`")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------------------------------- HTML

CSS = """
:root{color-scheme:light;--fg:#1c2330;--mut:#5c6675;--bg:#f6f7f9;--card:#fff;--line:#dfe3ea;--acc:#2456d6;
--ok:#14703a;--okbg:#e3f5ea;--bad:#a4281c;--badbg:#fde8e5;--warn:#8a5a00;--warnbg:#fff3d6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:24px;margin:0 0 4px;overflow-wrap:anywhere}h2{font-size:17px;margin:0 0 10px}
.sub{color:var(--mut);margin:0 0 18px;overflow-wrap:anywhere}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:0 0 14px}
code,pre{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}code{background:#eef0f4;padding:1px 5px;
border-radius:4px;overflow-wrap:anywhere}pre{background:#f1f3f7;border:1px solid var(--line);border-radius:8px;
padding:10px 12px;overflow-x:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}
.badge{display:inline-block;font-weight:600;border-radius:999px;padding:2px 11px;font-size:13px}
.high{background:var(--okbg);color:var(--ok)}.medium{background:var(--warnbg);color:var(--warn)}
.low,.unknown{background:var(--badbg);color:var(--bad)}
.alert{border-radius:8px;padding:10px 14px;margin:0 0 14px;background:var(--warnbg);color:var(--warn);
border:1px solid #ecd28c}.ok{color:var(--ok);font-weight:600}.no{color:var(--bad);font-weight:600}
.mut{color:var(--mut)}.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
a{color:var(--acc);overflow-wrap:anywhere}ul{margin:6px 0 0;padding-left:20px}li{margin:3px 0}
"""


def render_html(r: dict[str, Any]) -> str:
    e = lambda x: html.escape(str(x), quote=True)  # noqa: E731
    c, fx = r["culprit"], r["fix"]
    P: list[str] = []

    def table(head: list[str], rows: list[list[str]]) -> str:
        th = "".join(f"<th>{e(h)}</th>" for h in head)
        body = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in row) + "</tr>" for row in rows)
        return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'

    def sec(title: str, inner: str) -> None:
        P.append(f"<section><h2>{e(title)}</h2>{inner}</section>")

    title = f"AgentBisect report: {c.sha[:10]}" if c else "AgentBisect report: no culprit found"
    P.append(f"<h1>{e(title)}</h1><p class=\"sub\">Investigation <code>{e(r['id'])}</code> &middot; status "
             f"<code>{e(r['status'])}</code> &middot; range <code>{e((r['good_sha'] or '?')[:10])}</code>.."
             f"<code>{e((r['bad_sha'] or '?')[:10])}</code></p>")
    if not c:
        P.append("<section><p>No culprit was identified yet.</p></section>")
    else:
        conf = c.confidence or "unknown"
        P.append(f'<p>Confidence <span class="badge {e(conf if conf in ("high", "medium", "low") else "unknown")}">'
                 f"{e(conf)}</span></p>")
        if r["multi"]:
            P.append('<div class="alert"><strong>Warning: multiple regressions likely.</strong> Verdicts conflict or '
                     "the revert did not clear the regression, so this commit may be only one of several causes.</div>")
        elif r["low_conf"]:
            P.append('<div class="alert"><strong>Warning: low confidence.</strong> Treat this culprit as a lead, '
                     "not a proof.</div>")
        other = [f for f in r["flags"] if MULTI_REGRESSION not in f]
        if other:
            P.append(f'<p class="mut">Flags: {e("; ".join(other))}</p>')
        ck = f"<p>Entire checkpoint: <code>{e(r['culprit_checkpoint'])}</code></p>" if r["culprit_checkpoint"] else ""
        if r.get("throttle"):
            P.append(f'<p class="mut">{e(r["throttle"])}</p>')
        if r.get("signature"):
            sec("Failure signature", "".join(f"<p>{e(x)}</p>" for x in _signature_lines(r)))
        sec("Culprit commit", f"<p><code>{e(c.sha)}</code></p>{ck}")
        sec(_intent_label(r), f"<pre>{e(c.checkpoint_intent or '(none recorded)')}</pre>")
        syms = ", ".join(f"<code>{e(s)}</code>" for s in c.impacted_symbols) or "none found"
        sec("Semantic diff and impacted symbols",
            f"<pre>{e(c.semantic_diff or '(none recorded)')}</pre><p>Impacted symbols: {syms}</p>")
        div = "".join(f"<li>{e(d)}</li>" for d in c.trace_divergence)
        sec("Trace divergence (good vs bad, span by span)",
            f"<ul>{div}</ul>" if div else '<p class="mut">No span divergence was available.</p>')
        rv = r["revert"]
        res = {True: '<span class="ok">PASSED</span>: the regression disappears when the culprit is reverted on HEAD',
               False: '<span class="no">FAILED</span>: the regression remains after reverting',
               None: "inconclusive" if rv["ran"] else "not run"}[rv["passed"]]
        sec("Revert test", f"<p>{res}</p><p class=\"mut\">Pass rate: good commit {e(_fmt(rv['baseline']))}, culprit "
            f"{e(_fmt(rv['culprit_rate']))}, HEAD with culprit reverted {e(_fmt(rv['reverted']))}.</p>"
            + (f"<p class=\"mut\">Budget (median over repeats vs the good-commit baseline): {e(rv['budget'])}.</p>"
               if rv["budget"] else ""))
        if r["rounds"]:
            rows = [[e(k), e(v.get("eval_rounds")), e(v.get("cost_usd"))] for k, v in r["rounds"].items()
                    if isinstance(v, dict)]
            saved = (f"<p>Smart bisect saved {e(r['rounds']['saved'])} eval rounds.</p>"
                     if r["rounds"].get("saved") is not None else "")
            sec("Smart vs plain bisect", table(["strategy", "eval rounds", "cost (USD, estimate)"], rows) + saved)
        if not fx:
            sec("Fix", "<p>Fix: not attempted.</p>")
        else:
            v = '<span class="ok">verified</span>' if fx["passed"] else \
                '<span class="no">NOT verified</span> (draft PR: closest attempt shown)'
            it = {True: '<span class="ok">passed</span>', False: '<span class="no">FAILED</span>',
                  None: "not run"}[fx["intent_test"]]
            t = table(["metric", "value"], [["pass rate before (culprit)", e(_fmt(fx["before"]))],
                                            ["pass rate after (fix)", e(_fmt(fx["after"]))],
                                            ["good-commit baseline", e(_fmt(fx["baseline"]))],
                                            ["held-out score", e(_fmt(fx["held_out"]))], ["intent test", it]])
            ck = f"<p>Fix checkpoint: <code>{e(fx['checkpoint'])}</code></p>" if fx["checkpoint"] else ""
            d = f"<pre>{e(fx['diff'])}</pre>" if fx["diff"] else ""
            sec("Fix", f"<p>Branch <code>{e(fx['branch'])}</code> &middot; attempt {fx['attempt']} of "
                f"{fx['attempts']} &middot; {v}</p>{t}{ck}{d}")
        if r["steps"]:
            sec("Steps and context tokens", table(["step", "label", "context tokens"],
                [[e(s.get("step")), e(s.get("label")), e(s.get("tokens"))] for s in r["steps"]]))
        else:
            sec("Steps and context tokens", '<p class="mut">No context-token entries were logged.</p>')
        if r["commits"]:
            sec("Commits tested", table(["commit", "verdict", "pass rate", "repeats", "runs"],
                [[f"<code>{e(x['sha'][:10])}</code>", e(x["verdict"]), e(_fmt(x["pass_rate"])), e(x["repeats"]),
                  e(x["runs"])] for x in r["commits"]]))
        links = "".join(f'<li>{e(lbl)}: <code>{e(t)}</code> &middot; <a href="{e(trace_url(t))}" rel="noopener">'
                        f"{e(trace_url(t))}</a></li>" for lbl, t in r["traces"]) or "<li>none recorded</li>"
        cps = f"<li>culprit <code>{e(r['culprit_checkpoint'] or 'none found')}</code>: " \
              f"<code>entire checkpoint explain {e(c.sha)} --full</code></li>"
        if fx and fx["checkpoint"]:
            cps += f"<li>fix <code>{e(fx['checkpoint'])}</code>: " \
                   f"<code>entire checkpoint explain {e(fx['branch'])} --full</code></li>"
        sec("Links and evidence", "<p>neatlogs traces (the dashboard URL pattern is a best guess; the id is always "
            f"the source of truth):</p><ul>{links}</ul><p>Entire checkpoints (run the command in the target "
            f"repo):</p><ul>{cps}</ul>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>" + "".join(P) + "</main></body></html>\n")


# ------------------------------------------------------------------------------------------------- output

def write_reports(st: BisectState, out_dir: str | Path | None = None, fmt: str = "both",
                  repo: str | None = None) -> list[Path]:
    out = Path(out_dir) if out_dir else EVIDENCE_DIR
    out.mkdir(parents=True, exist_ok=True)
    r = build_report(st, repo)
    paths = []
    if fmt in ("md", "both"):
        paths.append(out / f"report-{st.id}.md")
        paths[-1].write_text(render_markdown(r), encoding="utf-8")
    if fmt in ("html", "both"):
        paths.append(out / f"report-{st.id}.html")
        paths[-1].write_text(render_html(r), encoding="utf-8")
    return paths


def pr_title(r: dict[str, Any], draft: bool) -> str:
    c = r["culprit"]
    return ("[DRAFT, fix not verified] " if draft else "") + f"Fix agent regression introduced by {c.sha[:10]}"


def open_pr(st: BisectState, repo: str, *, draft: bool | None = None, confirm: bool = False,
            run: Callable[..., Any] = subprocess.run, say: Callable[[str], None] = print) -> dict[str, Any]:
    """Push the best fix branch and `gh pr create`. DRY-RUN unless confirm=True. `draft=None` means draft
    exactly when the fix did not verify. `run` is injectable so tests never touch git remotes or gh."""
    r = build_report(st, repo)
    fx = r["fix"]
    if not r["culprit"] or not fx:
        raise ValueError("nothing to open a PR for: needs a culprit and at least one fix attempt (branch)")
    draft = (not fx["passed"]) if draft is None else draft
    title, body = pr_title(r, draft), render_markdown(r, pr=True)
    push = ["git", "-C", repo, "push", "-u", "origin", fx["branch"]]
    gh = ["gh", "pr", "create", "--head", fx["branch"], "--title", title, "--body-file", "-"] + \
         (["--draft"] if draft else [])
    result: dict[str, Any] = {"title": title, "body": body, "commands": [push, gh], "draft": draft,
                              "executed": False, "url": None}
    if not confirm:
        say("DRY RUN: nothing was pushed and no PR was created (pass --confirm-open-pr to do it).")
        say(f"title: {title}\ndraft: {draft}\ncommands:\n  " + "\n  ".join(" ".join(c) for c in result["commands"]))
        say("body:\n" + body)
        return result
    p = run(push, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git push failed: {(p.stderr or '').strip()}")
    g = run(gh, input=body, capture_output=True, text=True, cwd=repo)
    if g.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {(g.stderr or '').strip()}")
    result.update(executed=True, url=(g.stdout or "").strip())
    say(f"PR opened: {result['url']}")
    return result
