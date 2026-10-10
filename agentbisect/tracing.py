"""neatlogs instrumentation for AgentBisect itself. Every function no-ops without NEATLOGS_API_KEY or on failure."""
from __future__ import annotations

import contextlib
import os
from typing import Any, Iterator

WORKFLOW_NAME = "agentbisect"
_enabled = False


def init_tracing(investigation_id: str | None = None) -> bool:
    global _enabled
    if _enabled:
        return True
    if not os.environ.get("NEATLOGS_API_KEY"):
        return False
    try:
        import neatlogs

        tags = [f"inv:{investigation_id}"] if investigation_id else []
        neatlogs.init(api_key=os.environ["NEATLOGS_API_KEY"], workflow_name=WORKFLOW_NAME, tags=tags)
        _enabled = True
    except Exception:
        _enabled = False
    return _enabled


@contextlib.contextmanager
def step_span(name: str, **attrs: Any) -> Iterator[Any]:
    """One span per bisect step; attributes (e.g. context_tokens, sha) are best-effort. Yields a setter fn."""
    if not _enabled:
        yield lambda k, v: None
        return
    try:
        from neatlogs._wrap_utils import get_tracer

        cm = get_tracer().start_as_current_span(
            f"agentbisect.{name}", attributes={"openinference.span.kind": "CHAIN",
                                              **{k: v for k, v in attrs.items() if v is not None}})
    except Exception:
        yield lambda k, v: None
        return
    with cm as span:
        def setter(k: str, v: Any) -> None:
            try:
                span.set_attribute(k, v)
            except Exception:
                pass
        yield setter


def shutdown_tracing() -> None:
    global _enabled
    if not _enabled:
        return
    try:
        import neatlogs

        neatlogs.flush()
        neatlogs.shutdown()
    except Exception:
        pass
    _enabled = False
