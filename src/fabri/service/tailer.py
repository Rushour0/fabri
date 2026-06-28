"""B7 -- event stream by tailing the run's JSONL trace.

O2 (in-process streaming) is not built, so the service streams by *following*
the trace file the agent loop already writes (``log_event`` ->
``$FABRI_HOME/.fabri/traces/<session_id>.jsonl``), parsing each appended line
into the existing :mod:`fabri.events` vocabulary and yielding it live. This adds
nothing to the agent's hot path: a non-streaming run's JSONL is byte-identical
to today's.

:func:`tail_events` is transport-agnostic -- both the stdio and HTTP transports
consume the same generator. Termination is decided by an ``is_running``
predicate (typically ``RunHandle.is_running``): once the producing subprocess
has exited, we drain any remaining lines and stop, so a fully-written trace is
yielded in full even if the process finished before tailing began.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.orchestrator.traces import _SESSION_ID_RE

logger = get_logger()

# Terminal trace events: the agent loop emits a FINAL / FAILED / INCOMPLETE
# outcome event followed by a USAGE event (the run's cost surface). The CLI then
# appends an optional POST_RUN_USAGE for memory-compression cost. Tailing keys
# off the producing process exiting rather than any single event, so a host
# reading these doesn't have to know which event is truly last.
_TERMINAL_TYPES = frozenset(
    {EventType.FINAL.value, EventType.FAILED.value, EventType.INCOMPLETE.value}
)


def run_trace_path(fabri_home: str | Path, session_id: str) -> Path:
    """Resolve a run's trace path under *its own* ``FABRI_HOME``.

    The service may launch each run with a distinct ``FABRI_HOME`` (multi-tenant
    isolation), so we resolve the path explicitly rather than via the
    process-global :func:`fabri.paths.home`. ``session_id`` is validated against
    the same charset :func:`fabri.orchestrator.traces.trace_path` enforces so a
    host-supplied id can't escape the traces dir.
    """
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"invalid session_id {session_id!r}: must match {_SESSION_ID_RE.pattern}"
        )
    return Path(fabri_home).resolve() / ".fabri" / "traces" / f"{session_id}.jsonl"


def tail_events(
    path: str | Path,
    *,
    is_running: Callable[[], bool] | None = None,
    poll_interval: float = 0.05,
    timeout: float | None = None,
) -> Iterator[dict]:
    """Yield parsed trace events appended to ``path`` until the run ends.

    The run ends when ``is_running()`` returns ``False`` and the file is fully
    drained (or, when no predicate is given, when a terminal outcome event is
    seen). Partial trailing lines (a write caught mid-record) are buffered and
    re-read on the next poll. Malformed complete lines are skipped + logged,
    mirroring :func:`fabri.orchestrator.traces.read_trace`.

    ``timeout`` (seconds) bounds the total wait so a crashed producer that never
    writes a terminal event can't hang the caller forever; ``None`` waits
    indefinitely while ``is_running()`` stays true.
    """
    path = Path(path)
    deadline = None if timeout is None else time.monotonic() + timeout
    buf = ""
    pos = 0
    seen_terminal = False

    def _emit(chunk: str) -> Iterator[dict]:
        nonlocal buf, seen_terminal
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("trace tail %s: skipping malformed line: %s", path.name, e)
                continue
            if isinstance(event, dict) and event.get("type") in _TERMINAL_TYPES:
                seen_terminal = True
            yield event

    while True:
        if path.exists():
            text = path.read_text()
            if len(text) > pos:
                yield from _emit(text[pos:])
                pos = len(text)

        if is_running is None:
            if seen_terminal:
                return
        elif not is_running():
            # Producer exited -- do one final drain then stop.
            if path.exists():
                text = path.read_text()
                if len(text) > pos:
                    yield from _emit(text[pos:])
                    pos = len(text)
            return

        if deadline is not None and time.monotonic() >= deadline:
            logger.warning("trace tail %s: timed out after %.1fs", path.name, timeout)
            return
        time.sleep(poll_interval)


def extract_cost(events: list[dict]) -> dict:
    """Pull the run's cost surface out of the trace's ``usage`` event.

    Returns ``{cost_usd, subagent_cost_usd, total_cost_usd, post_run_cost_usd}``.
    ``cost_usd`` is the orchestrator's own COGS; ``total_cost_usd`` adds the
    sub-agent subtree (the number a host bills); ``post_run_cost_usd`` rolls in
    any ``post_run_usage`` (memory-compression) events the CLI appends after the
    run's ``usage`` event. Missing fields default to ``0.0`` / ``None`` so a
    host always gets a stable shape.
    """
    own = None
    subagent = None
    total = None
    post_run = 0.0
    for ev in events:
        etype = ev.get("type")
        if etype == EventType.USAGE.value:
            own = ev.get("cost_usd")
            subagent = ev.get("subagent_cost_usd")
            total = ev.get("total_cost_usd")
        elif etype == EventType.POST_RUN_USAGE.value:
            post_run += ev.get("cost_usd") or 0.0
    return {
        "cost_usd": own if own is not None else 0.0,
        "subagent_cost_usd": subagent if subagent is not None else 0.0,
        "total_cost_usd": total if total is not None else 0.0,
        "post_run_cost_usd": round(post_run, 6),
    }
