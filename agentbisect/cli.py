"""`agentbisect` entrypoint: investigate (intake + plain bisect), resume, status; watch is a later chunk."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
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
    if st.metrics.get("known_preexisting"):
        lines.append("  " + st.metrics["known_preexisting"]["message"])
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
    p.add_argument("--llm-proxy", default="off", metavar="auto|URL|off",
                   help="route the target's Anthropic calls through the local `claude`-CLI proxy: auto = start one "
                        "for this run, URL = use a running one, off (default) = use the real API key")
    p.add_argument("--signature", metavar="KEY",
                   help="bisect this failing check only (e.g. call_args:issue_refund.amount); default: discovered "
                        "from the bad end's eval results")
    p.add_argument("--all-signatures", action=argparse.BooleanOptionalAction, default=None,
                   help="bisect every distinct failing check at the bad end separately (one culprit per failure "
                        "signature). Default: on when several are found")
    p.add_argument("--all-categories", action="store_true",
                   help="build the suite from every category's non-held-out cases instead of the bad trace's "
                        "category only, so regressions in other features surface as their own signatures")
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
        return intake_trace(src, a.trace, a.repo, workflow=a.workflow,
                            all_categories=getattr(a, "all_categories", False))
    return intake_detection(src, a.detection, a.repo, workflow=a.workflow,
                            all_categories=getattr(a, "all_categories", False))


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
    rp = sub.add_parser("report", help="write the Markdown/HTML report; optionally open a PR (dry-run by default)")
    rp.add_argument("id")
    rp.add_argument("--format", choices=["md", "html", "both"], default="both")
    rp.add_argument("--out", help="output directory (default: evidence/ in the agentbisect repo)")
    rp.add_argument("--open-pr", action="store_true", help="prepare a PR from the best fix branch (dry-run)")
    rp.add_argument("--confirm-open-pr", action="store_true",
                    help="really `git push` and `gh pr create` (requires --open-pr)")
    rp.add_argument("--draft", action="store_true", help="force a draft PR (default: draft iff the fix is unverified)")
    cb = sub.add_parser("calibrate", help="run the good commit once (budget_repeats per case) and write the per-case "
                                          "median-token baseline that budget signatures are judged against")
    cb.add_argument("--repo", required=True, help="path to the target repo (has evals/cases.jsonl)")
    cb.add_argument("--good", required=True, help="the known-good commit to measure")
    cb.add_argument("--out", help="baseline file (default: <state-dir>/baseline-<good sha>.json)")
    cb.add_argument("--cases", help="comma-separated case ids (default: every case in evals/cases.jsonl)")
    cb.add_argument("--llm-proxy", default="off", metavar="auto|URL|off", help="as for `investigate`")
    fx = sub.add_parser("fix", help="run the Fixer on an investigation's culprit: a fresh agent edits a worktree "
                                    "(up to 3 attempts), writes a branch, patch and PR body; no push by default")
    fx.add_argument("id")
    fx.add_argument("--out", help="directory for the patch and PR body files (default: <state-dir>/fixes)")
    fx.add_argument("--max-attempts", type=int, default=3)
    fx.add_argument("--model", help="Fixer model (default $FIXER_MODEL or claude-sonnet-5-5)")
    fx.add_argument("--llm-proxy", default="off", metavar="auto|URL|off", help="as for `investigate` (eval runs)")
    fx.add_argument("--open-draft-pr", action="store_true",
                    help="really `git push` the fix branch and `gh pr create --draft` (default: write files only)")
    vf = sub.add_parser("verify-fix", help="verify a candidate fix commit: target signature gone, no regressions, "
                                           "held-out cases pass, culprit intent preserved (exit 0 verified, 1 not, "
                                           "2 inconclusive)")
    vf.add_argument("id")
    vf.add_argument("--commit", required=True, help="the candidate fix commit (or branch) in the target repo")
    vf.add_argument("--branch", default="", help="fix branch name (matches a recorded fix attempt)")
    vf.add_argument("--base", help="commit the fix was written against (default: the investigation's bad end)")
    vf.add_argument("--repo", help="repo holding the fix (default: the investigation's repo)")
    vf.add_argument("--llm-proxy", default="off", metavar="auto|URL|off", help="as for `investigate`")
    lp = sub.add_parser("llm-proxy", help="serve an Anthropic-compatible API backed by your logged-in `claude` CLI")
    lp.add_argument("--port", type=int, default=8787)
    lp.add_argument("--concurrency", type=int, default=None,
                    help="max model calls in flight (default $AGENTBISECT_MODEL_CONCURRENCY or 4)")
    lp.add_argument("--cache", action="store_true", help="cache responses in .agentbisect/llm_cache")
    lp.add_argument("--cache-dir")
    lp.add_argument("--debug-dump", metavar="DIR", help="DEBUG: write every rendered prompt + output to DIR (use /tmp)")
    js = sub.add_parser("judge-smoke", help="one Sonnet judge call through the proxy; prints the model recorded")
    js.add_argument("--proxy-url", help="running llm-proxy (default: start a throwaway one on your claude login)")
    sm = sub.add_parser("summary", help="per-signature table (culprit, revert test before/after, smart vs plain "
                                        "rounds) from the saved state of a re-run")
    sm.add_argument("dir", nargs="?", metavar="state_dir", help="state directory to read (default: --state-dir)")
    sm.add_argument("--id", help="only this investigation and its sibling signatures")
    sm.add_argument("--format", choices=["text", "md", "json"], default="text")
    sm.add_argument("--out", help="also write the table to this file")
    sub.add_parser("watch", help="poll neatlogs detections and auto-investigate (not implemented yet)")
    r = sub.add_parser("resume", help="resume an interrupted investigation")
    r.add_argument("id")
    s = sub.add_parser("status", help="show investigations")
    s.add_argument("id", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    store = StateStore(a.state_dir)
    if a.cmd == "llm-proxy":
        from .llm_proxy import run_forever
        return run_forever(a.port, a.concurrency, a.cache, a.cache_dir, debug_dump=a.debug_dump)
    if a.cmd == "judge-smoke":
        from .llm_proxy import judge_smoke
        try:
            r = judge_smoke(a.proxy_url)
        except Exception as e:
            print(f"judge-smoke failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(json.dumps(r, indent=2))
        return 0 if r["match"] else 1
    if a.cmd in ("investigate", "calibrate", "fix", "verify-fix"):
        with _llm_proxy(getattr(a, "llm_proxy", "off")):
            return _main(a, store)
    return _main(a, store)


@contextlib.contextmanager
def _llm_proxy(mode: str):
    """`auto` starts an in-process proxy for the duration; a URL is used as is. Runners read the env var."""
    px = None
    prev = os.environ.get("AGENTBISECT_LLM_PROXY_URL")
    if mode == "auto":
        from .llm_proxy import LLMProxy
        px = LLMProxy(cache_dir=".agentbisect/llm_cache")
        px.serve(0)
        os.environ["AGENTBISECT_LLM_PROXY_URL"] = px.url
        print(f"llm proxy (own claude login) at {px.url}", file=sys.stderr)
    elif mode and mode != "off":
        os.environ["AGENTBISECT_LLM_PROXY_URL"] = mode
    try:
        yield
    finally:
        if px:
            print(f"llm proxy stats: {json.dumps(px.stats.snapshot())}", file=sys.stderr)
            px.stop()
        if prev is None:
            os.environ.pop("AGENTBISECT_LLM_PROXY_URL", None)
        else:
            os.environ["AGENTBISECT_LLM_PROXY_URL"] = prev


def _main(a: argparse.Namespace, store: StateStore) -> int:
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
        if a.signature:
            from .signature import signature_from_key
            st.spec.signature = signature_from_key(a.signature)
            store.save(st)
        code = _run_one(store, st, meta)
        if a.all_signatures is not False:  # several failing checks at the bad end => one investigation each
            from .signature import spawn_siblings
            for sib in spawn_siblings(store, st):
                print(f"\nsibling investigation {sib.id} for failure signature {sib.spec.signature.label}")
                sib_code = _run_one(store, sib, meta)  # always run every sibling, even if the primary failed
                code = code or sib_code
        return code
    if a.cmd == "calibrate":
        return _calibrate(a, store)
    if a.cmd == "fix":
        return _fix(a, store)
    if a.cmd == "verify-fix":
        return _verify_fix(a, store)
    if a.cmd == "watch":
        print("agentbisect watch: not implemented yet (later chunk)", file=sys.stderr)
        return 2
    try:
        if a.cmd == "summary":
            return _summary_cmd(a, store)
        if a.cmd == "compare":
            return _compare(a, store)
        if a.cmd == "report":
            return _report(a, store)
        if a.cmd == "status":
            states = [store.load(a.id)] if a.id else store.list()
            if not states:
                print("no investigations")
            for st in states:
                print(_summary(st))
            return 0
        st = store.load(a.id)
        redo = store.resume(st)
        print(f"resumed {st.id}" + (f"; will redo interrupted step: {redo}" if redo else ""))
        return _run_one(store, st, st.metrics.get("source"))
    except StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _verify_fix(a: argparse.Namespace, store: StateStore) -> int:
    from .bisect import BisectError, make_runner
    from .verify import (ClaudeIntentJudge, VerifierInput, describe_verification, record_verification, verify_fix)
    try:
        st = store.load(a.id)
        runner = make_runner(store, st)
        vin = VerifierInput(repo=a.repo or st.repo, commit=a.commit, branch=a.branch, base=a.base)
        res = verify_fix(st, runner, vin, ClaudeIntentJudge(runner.cfg.models["judge_model"]))
        record_verification(store, st, vin, res)
    except (StateError, BisectError, ValueError, RuntimeError) as e:  # incl. BaselineError, bad shas, illegal state
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("\n".join(describe_verification(res)))
    return {True: 0, False: 1, None: 2}[res.passed]


def _calibrate(a: argparse.Namespace, store: StateStore) -> int:
    from pathlib import Path
    from .baseline import BaselineError, calibrate
    from .bisect import BisectError, resolve
    from .intake import load_cases
    from .runner import Runner, config_from_env
    try:
        good = resolve(a.repo, a.good)
        cases = [c.strip() for c in a.cases.split(",") if c.strip()] if a.cases else [c.id for c in load_cases(a.repo)]
        out = Path(a.out) if a.out else store.base / f"baseline-{good[:10]}.json"
        runner = Runner(a.repo, config_from_env(cases), store.cache(a.repo))
        data = calibrate(runner, good, cases, out)
    except (BaselineError, BisectError, IntakeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"calibrated {len(data['cases'])} cases at {good[:10]} ({data['repeats']} repeats, "
          f"agent {data['agent_model']}) -> {out}")
    for c, v in data["cases"].items():
        print(f"  {c}: median {v['median_tokens']:.0f} tokens, {v['median_steps']} steps (n={v['n']})")
    for w in data.get("warnings", []):
        print(f"  WARNING: {w}")
    for c, why in data["excluded_cases"].items():
        print(f"  {c}: excluded ({why})")
    print(f"use it:  export AGENTBISECT_BASELINE={Path(out).resolve()}")
    return 0


def _summary_cmd(a: argparse.Namespace, store: StateStore) -> int:
    from pathlib import Path
    from .summary import group_states, render, summary_rows
    store = StateStore(a.dir) if a.dir else store
    states = group_states(store, store.load(a.id)) if a.id else store.list()
    text = render(summary_rows(states), a.format)
    print(text)
    if a.out:
        Path(a.out).write_text(text if text.endswith("\n") else text + "\n")
    return 0


def _proxy_events() -> dict | None:
    url = os.environ.get("AGENTBISECT_LLM_PROXY_URL")
    if not url:
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/events", timeout=5) as f:
            return json.loads(f.read())
    except Exception:
        return None


def record_throttle(store: StateStore, st: BisectState, mark: dict | None) -> None:
    """Add what the LLM proxy logged since `mark` (a /events snapshot taken before the run) to
    metrics["throttle"], so a report can cite backoffs and recoveries as evidence. Counts are deltas, so
    siblings sharing a proxy and resumed runs neither double count nor lose earlier counts. No-op without a proxy."""
    now = _proxy_events()
    if not mark or not now or "totals" not in now or "totals" not in mark:
        return
    d = {k: now["totals"][k] - mark["totals"][k] for k in now["totals"]}
    if not any(d.values()):
        return
    t = st.metrics.setdefault("throttle", {"backoffs": 0, "recovered": 0, "gave_up": 0, "waited_s": 0.0, "events": []})
    t["backoffs"] += d["backoff"]
    t["recovered"] += d["recovered"]
    t["gave_up"] += d["gave_up"]
    t["waited_s"] = round(t["waited_s"] + d["waited_s"], 1)
    t["concurrency"] = now.get("concurrency")
    t["events"] = (t["events"] + [e for e in now.get("events", []) if e.get("seq", 0) >= mark["next_seq"]])[-50:]
    try:
        store.save(st)
    except OSError:
        pass


def _run_one(store: StateStore, st: BisectState, meta: dict | None) -> int:
    from .bisect import resume_investigation
    init_tracing(st.id)
    mark = _proxy_events()
    try:
        return _explain(store, st, int(resume_investigation(store, st, strategy=make_strategy(st, store, meta))), meta)
    finally:
        record_throttle(store, st, mark)
        shutdown_tracing()


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


def _fix(a: argparse.Namespace, store: StateStore) -> int:
    from .bisect import make_runner
    from .fixer import ProxyFixModel, RunnerChecker, run_fixer
    from .runner import DEFAULT_JUDGE_MODEL
    from .verify import ClaudeIntentJudge
    if a.max_attempts < 1:
        print("error: --max-attempts must be at least 1", file=sys.stderr)
        return 2
    try:
        st = store.load(a.id)
    except StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not st.culprit:
        print("error: investigation has no culprit to fix", file=sys.stderr)
        return 1
    try:
        store.set_status(st, "fixing")  # fixing -> fixing is allowed, so a re-run after a crash works
        try:  # the same pinned judge as verify-fix; unusable (e.g. haiku tier) => the intent check is inconclusive
            judge = ClaudeIntentJudge(os.environ.get("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL)
        except ValueError:
            judge = None
        res = run_fixer(st, model=ProxyFixModel(model=a.model), checker=RunnerChecker(make_runner(store, st), st.id),
                        out_dir=a.out or os.path.join(a.state_dir, "fixes"), max_attempts=a.max_attempts,
                        open_pr=a.open_draft_pr, judge=judge)
    except Exception as e:  # never leave the investigation stuck in `fixing`
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        if st.status == "fixing":
            store.set_status(st, "failed", f"fixer: {type(e).__name__}: {e}")
        return 1
    store.set_status(st, "verifying" if res.passed else "culprit_found")
    for i in res.attempts:
        print(f"attempt {i.attempt}: " + ("checks passed" if i.passed else f"failed ({i.error or '; '.join(i.failures[:2])})"))
    print(f"fix {'PASSED the Fixer checks' if res.passed else 'did NOT pass'}; branch={res.branch or '-'} "
          f"verifier_input={json.dumps(res.verifier_input)}")
    if res.eval_expectation_changes:
        from .verify import format_changes
        print("EXPECTATION CHANGED (human review required):")
        for line in format_changes(res.eval_expectation_changes):
            print(f"  {line}")
    print(f"patch: {res.patch_path or '-'}  pr body: {res.pr_body_path or '-'}  pr: {res.pr_url or 'not opened'}")
    return 0 if res.passed else 1


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
    st.metrics["comparison"] = res
    store.save(st)
    return 0


def _report(a: argparse.Namespace, store: StateStore) -> int:
    from .report import open_pr, write_reports
    if a.confirm_open_pr and not a.open_pr:
        print("error: --confirm-open-pr requires --open-pr", file=sys.stderr)
        return 2
    st = store.load(a.id)
    from .summary import group_states
    for p in write_reports(st, a.out, a.format, group=group_states(store, st)):
        print(f"wrote {p}")
    if a.open_pr:
        try:
            open_pr(st, st.repo, draft=True if a.draft else None, confirm=a.confirm_open_pr)
        except (ValueError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if a.confirm_open_pr and st.status in ("verifying", "fixing", "culprit_found"):
            store.set_status(st, "reported")
    return 0
