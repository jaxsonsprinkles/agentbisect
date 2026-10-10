"""Intake: turn a bad trace, a detection or a GitHub issue into a RegressionSpec (plus the good/bad commit range).

Trace sources are pluggable (`NeatlogsCliSource` shells out to the neatlogs public-API CLI, `FixtureSource` replays
recorded JSON). Eval cases come from the target repo's own `evals/cases.jsonl`; its `held_out` flag is the only
thing that decides the split, and held-out ids never enter the working suite (`cases`). Ground-truth tags
(`breaks`, `intent_for`) are never read or copied.

Commit identity of a trace comes from the tags the target agent writes (`commit:<sha>`, `run:<id>`, `case:<id>`,
from AGENTBISECT_COMMIT / AGENTBISECT_RUN_ID / AGENTBISECT_CASE), falling back to metadata keys of the same name.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import RegressionSpec

NEATLOGS_BIN = os.path.expanduser("~/.local/neatlogs-cli/bin/neatlogs")
NEATLOGS_HOST = "https://app.neatlogs.com"
NEATLOGS_PROFILE = "neatlogs-us"
DEFAULT_WORKFLOW = "support-agent"
FORBIDDEN_CASE_KEYS = ("breaks", "intent_for")  # ground truth; dropped defensively
BUDGET = {"max_steps": 6, "token_factor": 1.5}


class IntakeError(RuntimeError):
    pass


class WaitingOnHumanLogin(IntakeError):
    """neatlogs CLI reads need the user's device login; nothing the agent can do until a human approves it."""


# ---------------------------------------------------------------- traces

@dataclass
class Trace:
    id: str
    workflow: str = ""
    status: str = ""
    tags: list[str] = field(default_factory=list)
    started_at: str = ""
    input: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    passed: bool | None = None  # explicit pass/fail signal if the trace carries one

    def tag(self, key: str) -> str | None:
        """Value of a `key:value` tag (falls back to a metadata key of the same name)."""
        for t in self.tags:
            if t.startswith(key + ":"):
                return t[len(key) + 1:]
        v = self.metadata.get(key)
        return str(v) if v else None

    @property
    def commit(self) -> str | None:
        return self.tag("commit")

    @property
    def case(self) -> str | None:
        return self.tag("case")

    @property
    def run_id(self) -> str | None:
        return self.tag("run")

    @property
    def ok(self) -> bool:
        return self.status.lower() not in ("error", "failed", "failure")


def _unwrap(d: Any) -> Any:
    """CLI envelope is {"data": ..., "success": true}."""
    return d["data"] if isinstance(d, dict) and "data" in d and "success" in d else d


def _text_of(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("input", "message", "content", "text", "prompt", "query"):
            if k in v:
                return _text_of(v[k])
        return json.dumps(v, sort_keys=True)
    if isinstance(v, list):
        for m in reversed(v):  # chat messages: last user turn
            if isinstance(m, dict) and m.get("role", "user") == "user":
                return _text_of(m.get("content"))
        return _text_of(v[-1]) if v else None
    return str(v)


def trace_from_dict(d: dict[str, Any]) -> Trace:
    """Tolerant normaliser for CLI list/get items and fixture dicts (camelCase or snake_case)."""
    d = _unwrap(d)
    if isinstance(d.get("trace"), dict):
        d = {**d["trace"], **{k: v for k, v in d.items() if k != "trace"}}
    meta = d.get("metadata") or {}
    tags = d.get("tags") or []
    if isinstance(tags, dict):
        tags = [f"{k}:{v}" for k, v in tags.items()]
    passed = d.get("passed", meta.get("passed"))
    if passed is None:
        res = next((t.split(":", 1)[1] for t in tags if t.startswith("result:")), None)
        passed = {"pass": True, "fail": False}.get(res) if res else None
    return Trace(
        id=str(d.get("traceId") or d.get("trace_id") or d.get("id")),
        workflow=str(d.get("workflowName") or d.get("workflow") or ""),
        status=str(d.get("status") or ""),
        tags=[str(t) for t in tags],
        started_at=str(d.get("startedAt") or d.get("started_at") or ""),
        input=_text_of(d.get("input")),
        metadata=dict(meta),
        passed=passed,
    )


@dataclass
class Span:
    """One step of a trace, reduced to what divergence needs: ordered, with the tool(s) it called (if any)."""
    name: str
    kind: str = ""  # llm | tool | agent | ...
    tools: list[str] = field(default_factory=list)  # tool names chosen/executed in this step
    args: dict[str, Any] = field(default_factory=dict)  # arguments of the first tool call, when known
    text: str = ""  # short output/prompt text for context

    @property
    def signature(self) -> str:
        return f"tool:{','.join(self.tools)}" if self.tools else f"{self.kind or 'span'}:{self.name}"


def _tools_in(v: Any, depth: int = 0) -> list[tuple[str, dict]]:
    """Tool calls found in a payload: OpenAI `tool_calls`, Anthropic `tool_use` blocks, plain {tool|tool_name}."""
    out: list[tuple[str, dict]] = []
    if depth > 6 or v is None:
        return out
    if isinstance(v, list):
        for x in v:
            out += _tools_in(x, depth + 1)
    elif isinstance(v, dict):
        if v.get("type") == "tool_use" and v.get("name"):
            out.append((str(v["name"]), v.get("input") if isinstance(v.get("input"), dict) else {}))
        elif isinstance(v.get("function"), dict) and v["function"].get("name"):
            a = v["function"].get("arguments")
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except ValueError:
                    a = {}
            out.append((str(v["function"]["name"]), a if isinstance(a, dict) else {}))
        elif v.get("tool_name") or (v.get("tool") and isinstance(v["tool"], str)):
            out.append((str(v.get("tool_name") or v["tool"]), v.get("args") if isinstance(v.get("args"), dict) else {}))
        else:
            for x in v.values():
                out += _tools_in(x, depth + 1)
    return out


def span_from_dict(d: dict[str, Any]) -> Span:
    """Tolerant normaliser (fixture dicts and neatlogs span items; payload fields `input`/`output` optional)."""
    attrs = d.get("attributes") or {}
    kind = str(d.get("kind") or d.get("spanKind") or attrs.get("openinference.span.kind") or "").lower()
    raw = d.get("tools") or d.get("tool_calls") or []
    if raw and all(isinstance(t, str) for t in raw):  # recorded spans: plain tool names
        found = [(t, d.get("args") or {}) for t in raw]
    else:
        found = _tools_in(raw)
    if not found and (d.get("tool") or attrs.get("tool.name")):
        found = [(str(d.get("tool") or attrs.get("tool.name")), d.get("args") or {})]
    if not found:
        found = _tools_in(d.get("output"))
    return Span(name=str(d.get("name") or d.get("spanName") or ""), kind="tool" if found and not kind else kind,
                tools=[n for n, _ in found], args=dict(found[0][1]) if found else {},
                text=(_text_of(d.get("output")) or _text_of(d.get("input")) or "")[:500])


class TraceSource(ABC):
    @abstractmethod
    def get_trace(self, trace_id: str) -> Trace: ...

    def spans(self, trace_id: str) -> list[Span]:
        """Ordered steps of a trace (empty when the source cannot provide them)."""
        return []

    @abstractmethod
    def list_traces(self, workflow: str | None = None, limit: int = 200) -> list[Trace]: ...

    @abstractmethod
    def get_detection(self, detection_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def detection_hits(self, detection_id: str, limit: int = 20) -> list[str]:
        """Trace ids that tripped the detection, newest first."""

    def detection_trend(self, detection_id: str, start: str, end: str, granularity: str = "day") -> list[dict]:
        return []


Run = Callable[[list[str]], "tuple[int, str, str]"]
PROCESSING_RETRIES = 6  # TRACE_PROCESSING retries; waits 10s, 20s, ... (about 3.5 min total)
PROCESSING_WAIT_S = 10.0

_AUTH_CODES = {"UNAUTHENTICATED", "UNAUTHORIZED", "AUTH_REQUIRED", "NOT_AUTHENTICATED", "LOGIN_REQUIRED",
               "TOKEN_EXPIRED", "INVALID_TOKEN", "CREDENTIALS_REQUIRED", "NO_CREDENTIALS", "NOT_LOGGED_IN"}
_AUTH_TEXT = re.compile(r"unauthori[sz]ed|unauthenticated|not (logged|signed) in|auth login|login required|"
                        r"token (is )?(expired|invalid)|no credentials|missing credentials|\b401\b", re.I)


def _subprocess_run(argv: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


class NeatlogsCliSource(TraceSource):
    """Reads through the neatlogs public-API CLI (`traces list|get|payload`, `detections get|hits|trends`).

    The project key used by the SDK does not authenticate CLI reads; the user must have run
    `neatlogs --host ... --profile neatlogs-us --credential-source profile auth login --device`."""

    def __init__(self, project: str | None = None, binary: str = NEATLOGS_BIN, host: str = NEATLOGS_HOST,
                 profile: str = NEATLOGS_PROFILE, run: Run | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._sleep = sleep
        self.project = project or os.environ.get("NEATLOGS_PROJECT_ID")
        self.binary, self.host, self.profile = binary, host, profile
        self._run = run or _subprocess_run

    def _call(self, *args: str, retry: bool = True) -> Any:
        """One CLI call; a freshly ingested trace answers TRACE_PROCESSING ("still being finalized"), so retry it."""
        for attempt in range(PROCESSING_RETRIES + 1 if retry else 1):
            try:
                return self._call_once(*args)
            except IntakeError as e:
                if "TRACE_PROCESSING" not in str(e) or attempt >= PROCESSING_RETRIES or not retry:
                    raise
                self._sleep(PROCESSING_WAIT_S * (attempt + 1))

    def _call_once(self, *args: str) -> Any:
        argv = [self.binary, "--host", self.host, "--profile", self.profile, "--credential-source", "profile",
                "--json"]
        if self.project:
            argv += ["--project", self.project]
        try:
            rc, out, err = self._run(argv + list(args))
        except FileNotFoundError as e:
            raise IntakeError(f"neatlogs CLI not found at {self.binary}") from e
        except subprocess.TimeoutExpired as e:
            raise IntakeError(f"neatlogs CLI timed out: {' '.join(args)}") from e
        body: Any = None
        for blob in (out, err):
            try:
                body = json.loads(blob)
                break
            except (ValueError, TypeError):
                continue
        code = body.get("code") if isinstance(body, dict) else None
        if rc != 0 or code:
            text = f"{out}\n{err}"
            if code in _AUTH_CODES or (code is None and _AUTH_TEXT.search(text)):
                raise WaitingOnHumanLogin(
                    "waiting on human login: neatlogs CLI is not authenticated. Run "
                    f"`{self.binary} --host {self.host} --profile {self.profile} --credential-source profile "
                    "auth login --device` and approve the device code in the browser, then retry.")
            if code == "MISSING_PROJECT_ID":
                raise IntakeError("neatlogs project id missing: pass --project <uuid> or set NEATLOGS_PROJECT_ID")
            raise IntakeError(f"neatlogs CLI failed ({code or rc}): {(err or out).strip()[:300]}")
        return _unwrap(body)

    def _paged(self, key: str, *args: str, limit: int) -> list[dict]:
        data = self._call(*args, "--limit", str(min(limit, 50)), "--all", "--max-items", str(limit))
        return list(data.get(key, [])) if isinstance(data, dict) else list(data or [])

    def get_trace(self, trace_id: str) -> Trace:
        t = trace_from_dict(self._call("traces", "get", trace_id))
        if t.input is None:
            try:
                t.input = self._root_input(trace_id)
            except IntakeError:  # e.g. TRACE_PROCESSING: spans not readable yet; the case tag still identifies the case
                pass
        return t

    def _root_input(self, trace_id: str) -> str | None:
        spans = self._call("traces", "spans", "list", trace_id, "--limit", "50", retry=False)
        roots = [s for s in spans.get("spans", []) if not s.get("parentSpanId")] if isinstance(spans, dict) else []
        for s in roots[:1]:
            p = self._call("traces", "payload", trace_id, s["spanId"], "--fields", "input")
            v = ((p.get("fields") or {}).get("input") or {}) if isinstance(p, dict) else {}
            if v.get("available"):
                return _text_of(v.get("value"))
        return None

    def spans(self, trace_id: str) -> list[Span]:
        """Spans in start order; LLM/tool payloads are fetched to find chosen tools (shape UNVERIFIED on real runs)."""
        data = self._call("traces", "spans", "list", trace_id, "--limit", "50")
        items = data.get("spans", []) if isinstance(data, dict) else []
        items = sorted(items, key=lambda s: str(s.get("startTime") or s.get("startedAt") or ""))
        out = []
        for s in items:
            if not s.get("parentSpanId"):
                continue  # the root run span only wraps the steps
            d = dict(s)
            try:
                p = self._call("traces", "payload", trace_id, s["spanId"], "--fields", "output")
                v = ((p.get("fields") or {}).get("output") or {}) if isinstance(p, dict) else {}
                d["output"] = v.get("value") if v.get("available") else None
            except IntakeError:
                pass
            out.append(span_from_dict(d))
        return out

    def list_traces(self, workflow: str | None = None, limit: int = 200) -> list[Trace]:
        args = ["traces", "list"] + (["--workflow", workflow] if workflow else [])
        return [trace_from_dict(d) for d in self._paged("traces", *args, limit=limit)]

    def get_detection(self, detection_id: str) -> dict[str, Any]:
        return self._call("detections", "get", detection_id)

    def detection_hits(self, detection_id: str, limit: int = 20) -> list[str]:
        out = []
        for h in self._paged("hits", "detections", "hits", detection_id, limit=limit):
            tid = h.get("traceId") or h.get("trace_id") or (h.get("trace") or {}).get("traceId")
            if tid:
                out.append(str(tid))
        return out

    def detection_trend(self, detection_id: str, start: str, end: str, granularity: str = "day") -> list[dict]:
        d = self._call("detections", "trends", detection_id, "--from", start, "--to", end,
                       "--granularity", granularity)
        return list(d.get("buckets", [])) if isinstance(d, dict) else []


class FixtureSource(TraceSource):
    """Recorded JSON: {"traces": [trace dicts], "detections": {id: {..., "hits": [trace ids], "trend": [...]}}}.
    Pass a dict or a path to a .json file."""

    def __init__(self, data: dict[str, Any] | str | Path):
        if not isinstance(data, dict):
            data = json.loads(Path(data).read_text())
        self.traces = {t.id: t for t in map(trace_from_dict, data.get("traces", []))}
        self.detections: dict[str, dict] = data.get("detections", {})
        self._spans = {k: [span_from_dict(x) for x in v] for k, v in (data.get("spans") or {}).items()}
        self.fail_auth = bool(data.get("auth_failure"))

    def _check(self) -> None:
        if self.fail_auth:
            raise WaitingOnHumanLogin("waiting on human login (fixture auth_failure)")

    def get_trace(self, trace_id: str) -> Trace:
        self._check()
        if trace_id not in self.traces:
            raise IntakeError(f"trace {trace_id} not found")
        return self.traces[trace_id]

    def spans(self, trace_id: str) -> list[Span]:
        self._check()
        return list(self._spans.get(trace_id, []))

    def list_traces(self, workflow: str | None = None, limit: int = 200) -> list[Trace]:
        self._check()
        ts = [t for t in self.traces.values() if not workflow or t.workflow == workflow]
        return sorted(ts, key=lambda t: t.started_at, reverse=True)[:limit]

    def get_detection(self, detection_id: str) -> dict[str, Any]:
        self._check()
        if detection_id not in self.detections:
            raise IntakeError(f"detection {detection_id} not found")
        return {k: v for k, v in self.detections[detection_id].items() if k not in ("hits", "trend")}

    def detection_hits(self, detection_id: str, limit: int = 20) -> list[str]:
        self._check()
        return list(self.detections.get(detection_id, {}).get("hits", []))[:limit]

    def detection_trend(self, detection_id: str, start: str, end: str, granularity: str = "day") -> list[dict]:
        self._check()
        return list(self.detections.get(detection_id, {}).get("trend", []))


# ---------------------------------------------------------------- eval cases

@dataclass
class Case:
    id: str
    category: str
    input: str
    checks: dict[str, Any]
    held_out: bool = False
    requires_tool: str | None = None
    rubric: str | None = None


def load_cases(repo: str | Path) -> list[Case]:
    p = Path(repo) / "evals" / "cases.jsonl"
    if not p.exists():
        raise IntakeError(f"{p} not found: target repo has no eval cases")
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(Case(id=d["id"], category=d["category"], input=d["input"], checks=d.get("checks", {}),
                        held_out=bool(d.get("held_out")), requires_tool=d.get("requires_tool"),
                        rubric=d.get("rubric")))  # FORBIDDEN_CASE_KEYS are simply never copied
    return out


def build_predicate(cases: list[Case]) -> dict[str, Any]:
    """Deterministic checks first (per case), judge rubric second (only where a case has one), then the budget."""
    return {
        "order": ["checks", "budget", "rubric"],
        "checks": [{"case_id": c.id, "checks": c.checks} for c in cases],
        "budget": dict(BUDGET),
        "rubric": {c.id: c.rubric for c in cases if c.rubric} or None,
    }


def split_suite(cases: list[Case], category_ids: list[str] | set[str]) -> tuple[list[Case], list[Case]]:
    """Working suite = non-held-out cases of the given categories; held-out = every held_out case of the target."""
    cats = set(category_ids)
    working = [c for c in cases if c.category in cats and not c.held_out]
    held = [c for c in cases if c.held_out]
    assert not {c.id for c in working} & {c.id for c in held}, "held-out leaked into the working suite"
    return working, held


# ---------------------------------------------------------------- git helpers

def _git(repo: str | Path, *args: str) -> str | None:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def resolve_sha(repo: str | Path, ref: str | None) -> str | None:
    return _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") if ref else None


def is_ancestor(repo: str | Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


# ---------------------------------------------------------------- intake results

@dataclass
class IntakeResult:
    spec: RegressionSpec
    good_sha: str | None = None
    bad_sha: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"spec": self.spec.to_dict(), "good_sha": self.good_sha, "bad_sha": self.bad_sha,
                "notes": self.notes}


_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("a an the is are was were to of and or in on for with it my me i you your we our this that be not "
            "no do does did can could would should please have has had at by from as but so if then when what "
            "where how why agent support customer customers asked ask says say get got just still now".split())


def _tokens(s: str) -> set[str]:
    out = set()
    for w in _WORD.findall(s.lower()):
        if w not in _STOP and len(w) > 2:
            out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def _norm(s: str) -> str:
    return " ".join(_WORD.findall(s.lower()))


def match_case_by_input(text: str, cases: list[Case]) -> Case | None:
    n = _norm(text)
    for c in cases:
        if _norm(c.input) == n:
            return c
    return None


def _git_commit_set(repo: str | Path) -> list[str]:
    out = _git(repo, "rev-list", "HEAD")
    return out.splitlines() if out else []


def intake_trace(source: TraceSource, bad_trace_id: str, repo: str | Path, *, workflow: str = DEFAULT_WORKFLOW,
                 max_good: int = 5, source_kind: str = "trace") -> IntakeResult:
    cases = load_cases(repo)
    bad = source.get_trace(bad_trace_id)
    notes: list[str] = []

    case = next((c for c in cases if c.id == bad.case), None) if bad.case else None
    if case is None and bad.input:
        case = match_case_by_input(bad.input, cases)
    if case is None:
        raise IntakeError(f"cannot map trace {bad_trace_id} to an eval case "
                          f"(case tag: {bad.case!r}, input: {(bad.input or '')[:60]!r})")
    notes.append(f"trace maps to case {case.id} ({case.category})")

    bad_sha = resolve_sha(repo, bad.commit)
    if bad_sha is None:
        bad_sha = resolve_sha(repo, "HEAD")
        notes.append(f"trace has no usable commit tag ({bad.commit!r}); assuming HEAD as bad")

    # passing traces of the same case from strictly earlier commits, newest commit first, one per commit
    good_ids: list[str] = []
    good_sha: str | None = None
    seen: set[str] = set()
    ranked = {sha: i for i, sha in enumerate(_git_commit_set(repo))}  # lower index = newer
    cands = []
    for t in source.list_traces(workflow=workflow or None):
        if t.id == bad.id or t.case != case.id or not t.ok or t.passed is False:
            continue
        sha = resolve_sha(repo, t.commit)
        if not sha or sha == bad_sha or sha not in ranked or not is_ancestor(repo, sha, bad_sha or "HEAD"):
            continue
        cands.append((ranked[sha], sha, t))
    for _, sha, t in sorted(cands, key=lambda x: (x[0], x[2].started_at)):
        if sha in seen:
            continue
        seen.add(sha)
        good_ids.append(t.id)
        good_sha = good_sha or sha
        if len(good_ids) >= max_good:
            break
    if not good_ids:
        notes.append("no earlier passing traces found; bisect must establish the good end by re-running")
    elif not any(t.passed for _, _, t in cands):
        notes.append("good traces are 'status ok' only (no explicit pass signal); bisect re-confirms the good end")

    working, held = split_suite(cases, {case.category})
    if case.held_out:
        notes.append(f"{case.id} is held out: kept out of the working suite; related cases used instead")
    if not working:
        raise IntakeError(f"no non-held-out cases in category {case.category} to build a suite")
    spec = RegressionSpec(source=source_kind, bad_trace_id=bad.id, good_trace_ids=good_ids,
                          cases=[c.id for c in working], held_out=[c.id for c in held],
                          predicate=build_predicate(working), metric="pass_rate")
    return IntakeResult(spec, good_sha, bad_sha, notes)


def intake_detection(source: TraceSource, detection_id: str, repo: str | Path, *,
                     workflow: str = DEFAULT_WORKFLOW, max_good: int = 5) -> IntakeResult:
    det = source.get_detection(detection_id)
    hits = source.detection_hits(detection_id)
    if not hits:
        raise IntakeError(f"detection {detection_id} ({det.get('displayName', '?')}) has no hit traces yet")
    last_err = None
    for tid in hits:  # newest hit that maps to an eval case
        try:
            r = intake_trace(source, tid, repo, workflow=workflow, max_good=max_good, source_kind="detection")
        except IntakeError as e:
            last_err = e
            continue
        r.notes.insert(0, f"detection {det.get('displayName') or detection_id}: using hit trace {tid} "
                          f"({len(hits)} hits)")
        return r
    raise IntakeError(f"none of the {len(hits)} hit traces of detection {detection_id} maps to an eval case: "
                      f"{last_err}")


# ---------------------------------------------------------------- GitHub issue intake

class LLM(Protocol):
    """Injectable text-completion interface. Callers pass the pinned judge model (`judge_model()`)."""

    def complete(self, system: str, prompt: str, model: str) -> str: ...


def judge_model() -> str:
    return os.environ.get("JUDGE_MODEL", "claude-sonnet-5-5")


def fetch_issue(url: str) -> dict[str, Any]:
    p = subprocess.run(["gh", "issue", "view", url, "--json", "title,body,labels,number,url"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise IntakeError(f"gh issue view failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def extract_symptom(issue: dict[str, Any]) -> str:
    body = (issue.get("body") or "").strip()
    para = next((p.strip() for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith("#")), "")
    return f"{issue.get('title', '').strip()}. {para}".strip(" .") if para else issue.get("title", "").strip()


def match_cases_by_keyword(symptom: str, cases: list[Case]) -> list[str]:
    """Categories whose name or case inputs best overlap the symptom words (ties kept)."""
    sym = _tokens(symptom)
    scores: dict[str, int] = {}
    for cat in {c.category for c in cases}:
        words = _tokens(cat.replace("_", " "))
        for c in cases:
            if c.category == cat:
                words |= _tokens(c.input)
        scores[cat] = len(sym & words) + 2 * len(sym & _tokens(cat.replace("_", " ")))
    best = max(scores.values(), default=0)
    return sorted(c for c, s in scores.items() if s == best and s > 0)


def _llm_categories(llm: LLM, symptom: str, cases: list[Case]) -> tuple[str, list[str]]:
    cats = sorted({c.category for c in cases})
    system = ("Extract the user-visible symptom from a bug report about a support agent and pick which eval "
              "categories it concerns. Reply with JSON only: {\"symptom\": str, \"categories\": [str]}.")
    raw = llm.complete(system, f"Categories: {cats}\n\nReport:\n{symptom}", judge_model())
    try:
        d = json.loads(raw)
        return str(d.get("symptom") or symptom), [c for c in d.get("categories", []) if c in cats]
    except (ValueError, AttributeError):
        return symptom, []


def intake_issue(issue: dict[str, Any] | str, repo: str | Path, *, llm: LLM | None = None) -> IntakeResult:
    if isinstance(issue, str):
        issue = fetch_issue(issue)
    cases = load_cases(repo)
    symptom = extract_symptom(issue)
    notes = [f"issue #{issue.get('number', '?')}: {symptom[:160]}"]
    cats: list[str] = []
    if llm is not None:
        symptom, cats = _llm_categories(llm, symptom, cases)
        notes.append(f"categories via judge model {judge_model()}: {cats}")
    if not cats:
        cats = match_cases_by_keyword(symptom, cases)
        notes.append(f"categories via keyword match: {cats}")
    if not cats:
        raise IntakeError("could not map the issue symptom to any eval category")
    working, held = split_suite(cases, set(cats))
    if not working:
        raise IntakeError(f"categories {cats} have no non-held-out cases")
    spec = RegressionSpec(source="issue", bad_trace_id=None, good_trace_ids=[],
                          cases=[c.id for c in working], held_out=[c.id for c in held],
                          predicate=build_predicate(working), metric="pass_rate")
    return IntakeResult(spec, None, resolve_sha(repo, "HEAD"), notes)
