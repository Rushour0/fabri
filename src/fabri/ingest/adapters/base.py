"""The one adapter contract every log source is normalized through.

An adapter turns a raw log into fabri **events** — the exact same
``EventType``-keyed dicts the agent loop writes to a trace — grouped into
``Session``s. The Improver then feeds each session's events straight into
``process_trace(events=...)`` (orchestrator/pipeline.py), so external logs mine
into memory through the identical failure/success/postmortem miners fabri uses
on its own runs. There is no second schema and no second learning path.

Three authoring styles all satisfy this one ``Adapter`` protocol:
  - a plain ``@fabri.adapter`` function or class (this module's ``Session`` +
    the ``*_event`` builders),
  - ``ConfigMapAdapter`` (declarative YAML field-map, zero code),
  - ``ToolAdapter`` (a polyglot executable via the tool contract).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator, Protocol, runtime_checkable

from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.tools.result import tool_error, tool_ok

if TYPE_CHECKING:
    from fabri.ingest.sources import LogSource

logger = get_logger()


@dataclass
class Session:
    """One delimited unit of work an adapter carves out of a log — the analogue
    of a single fabri run. ``events`` are plain dicts in the ``EventType``
    vocabulary; ``session_id`` tags provenance on every ingested guideline."""

    session_id: str
    events: list[dict] = field(default_factory=list)


@runtime_checkable
class Adapter(Protocol):
    name: str

    def sessions(self, source: "LogSource", options: dict) -> Iterator[Session]:
        ...


# --- event builders: authors emit normalized events without hand-writing dicts ---

def start_event(task: str) -> dict:
    """The required per-session opener. ``build_postmortem_text`` and the
    success miner both key off ``task``, so every session should start with one."""
    return {"type": EventType.START.value, "task": task or ""}


def tool_event(
    name: str,
    args: dict | None = None,
    *,
    ok: bool = True,
    error: str | None = None,
    result: dict | None = None,
) -> dict:
    """The mineable core. ``ok=False``/``error`` set → mined as a failure;
    ``ok=True`` → contributes to the success pattern. ``result`` is built via
    the canonical ``tool_ok``/``tool_error`` factories so the ``ok`` boolean is
    always present and correct (a missing ``result`` would be read as a
    failure by ``is_error``)."""
    res = tool_ok(result) if ok else tool_error(error or "unknown error", result)
    return {"type": EventType.TOOL_CALL.value, "name": name, "args": args or {}, "result": res}


def final_event(outcome: str = "success") -> dict:
    """Recommended terminator — the success miner only fires when a ``final``
    event exists, so omitting it yields postmortems + failure guidelines but no
    success patterns."""
    return {"type": EventType.FINAL.value, "outcome": outcome}


# Session ids may be built from arbitrary log fields; sanitize to the charset
# traces._SESSION_ID_RE accepts so an id that ever hits disk (or is logged)
# can't smuggle a path traversal.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_session_id(adapter: str, key: str) -> str:
    cleaned = _UNSAFE.sub("-", f"{adapter}-{key}").strip("-")
    return (cleaned or adapter)[:120]


def valid_event(event: dict) -> bool:
    """Minimal well-formedness, mirroring ``read_trace``'s tolerance: every event
    needs a ``type``; a ``tool_call`` additionally needs ``name`` + ``result``."""
    if not isinstance(event, dict) or "type" not in event:
        return False
    if event["type"] == EventType.TOOL_CALL.value:
        return "name" in event and isinstance(event.get("result"), dict)
    return True


def normalize_events(events: list[dict]) -> tuple[list[dict], int]:
    """Drop malformed events (skip+log like ``read_trace``); return
    ``(kept, skipped_count)`` so the Improver can surface skips in the summary."""
    kept, skipped = [], 0
    for e in events:
        if valid_event(e):
            kept.append(e)
        else:
            skipped += 1
            logger.warning("ingest: skipping malformed event: %r", e)
    return kept, skipped
