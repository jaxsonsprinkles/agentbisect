"""Durable state: investigation JSON + per-repo verdict cache under .agentbisect/, always written atomically."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import BisectState, CommitResult, STATUSES
from .tracing import step_span

DEFAULT_DIR = ".agentbisect"
BUILD_CASE = "__build__"

TRANSITIONS = {
    "created": {"running", "failed"},
    "running": {"running", "paused", "failed", "culprit_found", "refused"},  # running->running: resume after a crash
    "paused": {"running", "failed"},
    "failed": {"running"},
    "refused": {"running"},  # a known pre-existing failure was correctly refused; resume re-checks it
    "culprit_found": {"fixing", "verifying", "reported", "running"},
    "fixing": {"verifying", "failed", "paused", "reported"},
    "verifying": {"fixing", "reported", "failed", "paused"},
    "reported": set(),
}


class StateError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: Any) -> None:
    """Write tmp file in the same dir, fsync, then rename over the target: readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with _suppress():
            os.unlink(tmp)
        raise


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def cache_key(repo: str, sha: str, case: str, models: dict[str, str], repeat: int | None = None) -> str:
    """(commit, case[, repeat], pinned models). v3: one record per repeat, so repeat i of a case at a commit is a
    distinct, resumable unit (and a different sample from repeat j)."""
    k = "|".join(["v3", sha, case, models.get("agent_model", ""), models.get("judge_model", "")])
    return k if repeat is None else f"{k}|r{repeat}"


class VerdictCache:
    """Per (repo, sha, case, repeat, pinned model ids) run records, reusable across investigations on one repo.

    Run record: {"status": "pass"|"fail", "trace_id": str|None, "check_fails": [keys], "check_info": {key: record},
    "tokens", "steps", "max_steps"} (the last three feed the budget median rule; absent in older records).
    `get` folds the contiguous repeats 0..n-1 into the aggregate the runner reads:
    {"passes", "fails", "trace_ids", "check_fails": {key: failing runs}, "check_info", "runs": n,
    "detail": [per-repeat {status, tokens, steps, max_steps, fails: [keys]}]}. A build-failure
    skip is stored under case '__build__' (and a target-side case skip under the case) as {"skip": reason}.
    Thread-safe; flushed to disk after every update."""

    def __init__(self, base: Path, repo: str):
        self.repo = str(Path(repo).resolve()) if repo else ""
        h = hashlib.sha256(self.repo.encode()).hexdigest()[:12]
        self.path = Path(base) / "cache" / f"{h}.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, sha: str, case: str, models: dict[str, str]) -> dict[str, Any] | None:
        with self._lock:
            skip = self._data.get(cache_key(self.repo, sha, case, models))
            if skip is not None:
                return dict(skip)
            agg: dict[str, Any] = {"passes": 0, "fails": 0, "trace_ids": [], "check_fails": {}, "check_info": {},
                                   "runs": 0, "detail": []}
            i = 0
            while (r := self._data.get(cache_key(self.repo, sha, case, models, i))) is not None:
                agg["passes" if r["status"] == "pass" else "fails"] += 1
                if r.get("trace_id"):
                    agg["trace_ids"].append(r["trace_id"])
                agg["detail"].append({"status": r["status"], "tokens": r.get("tokens"), "steps": r.get("steps"),
                                      "max_steps": r.get("max_steps"), "fails": list(r.get("check_fails", []))})
                for k in r.get("check_fails", []):
                    agg["check_fails"][k] = agg["check_fails"].get(k, 0) + 1
                for k, info in (r.get("check_info") or {}).items():
                    agg["check_info"].setdefault(k, info)
                i += 1
            agg["runs"] = i
            return json.loads(json.dumps(agg)) if i else None

    def add_run(self, sha: str, case: str, models: dict[str, str], repeat: int, status: str,
                trace_id: str | None = None, failures: dict[str, dict[str, Any]] | None = None,
                tokens: int | None = None, steps: int | None = None, max_steps: int | None = None) -> None:
        """Record repeat `repeat` of `case` at `sha` (idempotent: re-recording overwrites that repeat only)."""
        with self._lock:
            self._data[cache_key(self.repo, sha, case, models, repeat)] = {
                "status": status, "trace_id": trace_id, "check_fails": sorted(failures or {}),
                "check_info": dict(failures or {}), "tokens": tokens, "steps": steps, "max_steps": max_steps}
            atomic_write_json(self.path, self._data)

    def set_case_skip(self, sha: str, case: str, models: dict[str, str], reason: str) -> None:
        with self._lock:
            self._data[cache_key(self.repo, sha, case, models)] = {"skip": reason}
            atomic_write_json(self.path, self._data)

    def set_skip(self, sha: str, models: dict[str, str], reason: str) -> None:
        self.set_case_skip(sha, BUILD_CASE, models, reason)


class StateStore:
    """Owns .agentbisect/: investigations/<id>.json and cache/<repo-hash>.json."""

    def __init__(self, base: str | Path = DEFAULT_DIR):
        self.base = Path(base)

    def _path(self, inv_id: str) -> Path:
        return self.base / "investigations" / f"{inv_id}.json"

    def cache(self, repo: str) -> VerdictCache:
        return VerdictCache(self.base, repo)

    def create(self, repo: str, **fields: Any) -> BisectState:
        st = BisectState(id=uuid.uuid4().hex[:10], repo=str(Path(repo).resolve()), created_at=now(), **fields)
        self.save(st)
        return st

    def save(self, st: BisectState) -> None:
        st.updated_at = now()
        atomic_write_json(self._path(st.id), st.to_dict())

    def load(self, inv_id: str) -> BisectState:
        p = self._path(inv_id)
        if not p.exists():
            raise StateError(f"no investigation '{inv_id}' in {p.parent}")
        return BisectState.from_dict(json.loads(p.read_text()))

    def list(self) -> list[BisectState]:
        d = self.base / "investigations"
        out = [BisectState.from_dict(json.loads(p.read_text())) for p in sorted(d.glob("*.json"))] if d.exists() else []
        return sorted(out, key=lambda s: s.created_at)

    # -- transitions and steps (each persists immediately) --

    def set_status(self, st: BisectState, status: str, error: str | None = None) -> None:
        if status not in STATUSES:
            raise StateError(f"unknown status '{status}'")
        if status not in TRANSITIONS[st.status]:
            raise StateError(f"illegal status transition {st.status} -> {status}")
        st.status = status
        st.error = error
        self.save(st)

    def begin_step(self, st: BisectState, kind: str, **info: Any) -> int:
        """Persist 'step N is in flight' BEFORE doing the work; a crash leaves it set so resume can redo the step."""
        st.in_flight = {"step": st.step + 1, "kind": kind, "started_at": now(), **info}
        self.save(st)
        return st.step + 1

    def end_step(self, st: BisectState, sha: str | None = None, result: CommitResult | None = None) -> None:
        if sha and result:
            st.results[sha] = result
        st.step += 1
        st.in_flight = None
        self.save(st)

    def record_result(self, st: BisectState, sha: str, result: CommitResult) -> None:
        st.results[sha] = result
        self.save(st)

    def log_context_tokens(self, st: BisectState, label: str, tokens: int) -> None:
        """Context tokens consumed by a step (with/without entire-graph is distinguished by label)."""
        st.context_tokens_per_step.append({"step": st.step + 1, "label": label, "tokens": int(tokens)})
        self.save(st)
        with step_span("context", label=label, context_tokens=int(tokens), step=st.step + 1):
            pass

    def interrupted(self, st: BisectState) -> bool:
        return st.status == "running" and st.in_flight is not None

    def resume(self, st: BisectState) -> dict[str, Any] | None:
        """Prepare a loaded investigation to continue. Returns the in-flight step that must be redone, if any."""
        if st.status == "reported":
            raise StateError("investigation already reported; nothing to resume")
        redo = st.in_flight
        self.set_status(st, "running")
        return redo
