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
    "running": {"running", "paused", "failed", "culprit_found"},  # running->running: resume after a crash
    "paused": {"running", "failed"},
    "failed": {"running"},
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


def cache_key(repo: str, sha: str, case: str, models: dict[str, str]) -> str:
    # v2: entries also hold per-check failures, so verdicts can be re-read for any failure signature
    return "|".join(["v2", sha, case, models.get("agent_model", ""), models.get("judge_model", "")])


class VerdictCache:
    """Per (repo, sha, case, pinned model ids) run tallies, reusable across investigations on the same repo.

    Entry: {"passes": n, "fails": n, "trace_ids": [...], "check_fails": {key: failing runs}, "check_info":
    {key: first failure record}}; the build-failure skip is stored under case '__build__'
    as {"skip": reason}. Thread-safe; flushed to disk after every update."""

    def __init__(self, base: Path, repo: str):
        self.repo = str(Path(repo).resolve()) if repo else ""
        h = hashlib.sha256(self.repo.encode()).hexdigest()[:12]
        self.path = Path(base) / "cache" / f"{h}.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, sha: str, case: str, models: dict[str, str]) -> dict[str, Any] | None:
        with self._lock:
            e = self._data.get(cache_key(self.repo, sha, case, models))
            return json.loads(json.dumps(e)) if e is not None else None

    def add_runs(self, sha: str, case: str, models: dict[str, str], passes: int, fails: int,
                 trace_ids: list[str], check_fails: dict[str, int] | None = None,
                 check_info: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            e = self._data.setdefault(cache_key(self.repo, sha, case, models),
                                      {"passes": 0, "fails": 0, "trace_ids": [], "check_fails": {}, "check_info": {}})
            e["passes"] += passes
            e["fails"] += fails
            e["trace_ids"].extend(trace_ids)
            for k, n in (check_fails or {}).items():
                e["check_fails"][k] = e["check_fails"].get(k, 0) + n
            for k, info in (check_info or {}).items():
                e["check_info"].setdefault(k, info)
            atomic_write_json(self.path, self._data)
            return json.loads(json.dumps(e))

    def set_case_skip(self, sha: str, case: str, models: dict[str, str], reason: str) -> None:
        with self._lock:
            self._data[cache_key(self.repo, sha, case, models)] = {"skip": reason}
            atomic_write_json(self.path, self._data)

    def set_skip(self, sha: str, models: dict[str, str], reason: str) -> None:
        with self._lock:
            self._data[cache_key(self.repo, sha, BUILD_CASE, models)] = {"skip": reason}
            atomic_write_json(self.path, self._data)


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
