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
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.orchestrator.traces import _SESSION_ID_RE
from fabri.service.trace_store import READ_CAP_BYTES, TraceReader

logger = get_logger()

# Terminal trace events: the agent loop emits a FINAL / FAILED / INCOMPLETE
# outcome event followed by a USAGE event (the run's cost surface). The CLI then
# appends an optional POST_RUN_USAGE for memory-compression cost. Tailing keys
# off the producing process exiting rather than any single event, so a host
# reading these doesn't have to know which event is truly last.
# Longest trace line the tailer parses. A longer line is dropped, so one
# runaway write (or a sparse file with no newline) cannot grow the buffer
# without bound.
MAX_LINE_BYTES = 1 << 20
_READS_PER_POLL = 16
# Most bytes one tail_events call reads. A trace past this (a runaway or
# sparse file) ends the tail with a warning instead of reading forever.
MAX_TAIL_BYTES = 512 << 20
# A dropped line still reports its event type, peeked from its first bytes,
# so a dropped terminal event still ends the stream.
_PEEK_BYTES = 4096
# Anchored to the line start so a nested "type" key, or text from the middle
# of a payload, is never read as the event type.
# The top-level "type" of a trace event. Real events start with scalar keys
# (ts, event_id) before "type", so leading scalar keys are skipped, but a
# nested object or array before "type" stops the match: a nested
# {"type": "final"} inside a big payload is never read as the event type.
_TYPE_RE = re.compile(
    rb'^\s*\{(?:\s*"\w+"\s*:\s*(?:"[^"\\]*"|[-\d.eE+]+|true|false|null)\s*,)*'
    rb'\s*"type"\s*:\s*"(\w+)"'
)

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

    The home is made absolute but not resolved: resolving would follow a
    symlinked run home before :mod:`fabri.service.trace_store` gets the
    chance to refuse it.
    """
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"invalid session_id {session_id!r}: must match {_SESSION_ID_RE.pattern}"
        )
    return Path(os.path.abspath(fabri_home)) / ".fabri" / "traces" / f"{session_id}.jsonl"


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
    # One descriptor for the whole tail, opened without following links and
    # checked (regular file, service uid, one link). A symlinked or
    # hardlinked trace raises TraceOpenError; swapping the path after the
    # open changes nothing because the reader never reopens by path.
    reader = TraceReader(path)
    # The unterminated tail of the last chunk. Bounded by MAX_LINE_BYTES: a
    # longer line is dropped (with a warning) up to its newline.
    buf = b""
    dropping = False
    drop_head = b""
    drop_bytes = 0
    truncations = 0
    total_read = 0
    stopped = False
    seen_terminal = False

    def _dropped(head: bytes, size: int) -> dict:
        """The marker yielded in place of a line over MAX_LINE_BYTES."""
        nonlocal seen_terminal
        logger.warning("trace tail %s: dropped a %d-byte line (cap %d)", path.name, size, MAX_LINE_BYTES)
        m = _TYPE_RE.match(head[:_PEEK_BYTES])
        etype = m.group(1).decode("ascii", "replace") if m else "dropped"
        if etype in _TERMINAL_TYPES:
            seen_terminal = True
        return {"type": etype, "truncated": True, "bytes": size}

    def _parse(raw: bytes) -> dict | None:
        nonlocal seen_terminal
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("trace tail %s: skipping malformed line: %s", path.name, e)
            return None
        if isinstance(event, dict) and event.get("type") in _TERMINAL_TYPES:
            seen_terminal = True
        return event

    def _emit(chunk: bytes) -> Iterator[dict]:
        nonlocal buf, dropping, drop_head, drop_bytes
        # Split once per chunk and carry only the last fragment, so a chunk
        # of n lines costs O(chunk) rather than O(n * chunk).
        lines = chunk.split(b"\n")
        tail = lines.pop()
        for i, raw in enumerate(lines):
            if i == 0:
                if dropping:
                    dropping = False
                    buf = b""
                    yield _dropped(drop_head, drop_bytes + len(raw))
                    continue
                raw = buf + raw
                buf = b""
            if len(raw) > MAX_LINE_BYTES:
                yield _dropped(raw[:_PEEK_BYTES], len(raw))
                continue
            event = _parse(raw)
            if event is not None:
                yield event
        if lines:
            buf = b""
        if dropping:
            drop_bytes += len(tail)
            return
        if len(buf) + len(tail) > MAX_LINE_BYTES:
            dropping = True
            drop_head = (buf + tail[:_PEEK_BYTES])[:_PEEK_BYTES]
            drop_bytes = len(buf) + len(tail)
            buf = b""
        else:
            buf += tail

    def _drain(max_reads: int | None = None) -> Iterator[dict]:
        nonlocal buf, dropping, truncations, total_read, stopped
        reads = 0
        while max_reads is None or reads < max_reads:
            reads += 1
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning("trace tail %s: timed out after %.1fs", path.name, timeout)
                stopped = True
                return
            if total_read >= MAX_TAIL_BYTES:
                logger.warning("trace tail %s: stopped after reading %d bytes (cap %d)",
                               path.name, total_read, MAX_TAIL_BYTES)
                stopped = True
                yield {"type": "dropped", "reason": "cap", "truncated": True}
                return
            chunk = reader.read_new(min(READ_CAP_BYTES, MAX_TAIL_BYTES - total_read))
            total_read += len(chunk)
            if reader.truncations != truncations:
                truncations = reader.truncations
                logger.warning("trace tail %s: file was truncated; reading again from the start",
                               path.name)
                buf = b""
                dropping = False
            if not chunk:
                return
            yield from _emit(chunk)

    try:
        while True:
            # Bounded per poll so a producer that writes faster than we read
            # still lets the timeout and is_running checks run.
            yield from _drain(_READS_PER_POLL)
            if stopped:
                return

            if is_running is None:
                if seen_terminal:
                    return
            elif not is_running():
                # Producer exited -- do one final drain then stop. The
                # drain still honours the deadline and the byte cap.
                yield from _drain()
                if dropping and not stopped:
                    # The file ended inside an oversized line: still report it.
                    dropping = False
                    yield _dropped(drop_head, drop_bytes)
                return

            if deadline is not None and time.monotonic() >= deadline:
                logger.warning("trace tail %s: timed out after %.1fs", path.name, timeout)
                return
            time.sleep(poll_interval)
    finally:
        reader.close()


def extract_cost(events: list[dict]) -> dict:
    """Pull the run's cost + metrics surface out of the trace's ``usage`` event.

    Returns a stable dict a host (and Studio) can render directly:
    ``cost_usd`` is the orchestrator's own COGS; ``total_cost_usd`` adds the
    sub-agent subtree (the number a host bills); ``post_run_cost_usd`` rolls in
    any ``post_run_usage`` (memory-compression) events the CLI appends after the
    run's ``usage`` event.

    Also surfaces the fields the ``usage`` event already carries but the service
    used to drop: ``cost_by_model`` (per-model COGS breakdown), token counts,
    ``step_count`` / ``wall_time_s``, and the sub-agent metrics. There is no
    budget field in the ``usage`` event -- a budget breach surfaces as the run's
    ``outcome`` (``budget_exceeded``), so a UI compares ``total_cost_usd`` to the
    ``max_cost_usd`` it knows from config rather than reading it here. Missing
    fields default to ``0.0`` / ``{}`` / ``None`` so the shape is always stable.
    """
    own = None
    subagent = None
    total = None
    post_run = 0.0
    cost_by_model: dict = {}
    metrics: dict = {}
    for ev in events:
        etype = ev.get("type")
        if etype == EventType.USAGE.value:
            own = ev.get("cost_usd")
            subagent = ev.get("subagent_cost_usd")
            total = ev.get("total_cost_usd")
            cost_by_model = ev.get("cost_by_model") or {}
            # Carry the run-quality metrics through verbatim (present since the
            # agent loop's usage event; see fabri.core.agent). Absent keys stay
            # out of the dict rather than defaulting, so a host can tell "not
            # reported" from "zero".
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "step_count",
                "wall_time_s",
                "subagent_count",
                "subagent_successful_count",
                "subagent_failed_count",
                "subagent_regret_count",
                "guideline_reuse_rate",
                "guidelines_from_prior_sessions",
            ):
                if key in ev:
                    metrics[key] = ev.get(key)
        elif etype == EventType.POST_RUN_USAGE.value:
            post_run += ev.get("cost_usd") or 0.0
    return {
        "cost_usd": own if own is not None else 0.0,
        "subagent_cost_usd": subagent if subagent is not None else 0.0,
        "total_cost_usd": total if total is not None else 0.0,
        "post_run_cost_usd": round(post_run, 6),
        "cost_by_model": cost_by_model,
        "metrics": metrics,
    }
