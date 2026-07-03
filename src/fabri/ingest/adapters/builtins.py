"""The bundled adapters, registered through the same ``fabri.adapters``
entry-point group third parties use (fabri dogfoods its own plugin mechanism):

  - ``jsonl``  — native/aggregated fabri events, passthrough (+ optional regroup).
  - ``regex``  — generic plaintext, one named-capture regex maps line → fields.
  - ``otel``   — OTel-span / OpenAI-response shaped JSON records.
"""
from __future__ import annotations

import re
from typing import Iterator

from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.ingest.adapters.base import (
    Session,
    final_event,
    safe_session_id,
    start_event,
    tool_event,
)
from fabri.ingest.sources import LogSource, _dotted

logger = get_logger()

# Words in a captured "status" group that mean the tool call failed.
_FAIL_WORDS = {"error", "fail", "failed", "failure", "err", "false", "ko", "exception"}


def _ok_from_status(status: str | None) -> bool:
    if status is None:
        return True
    return status.strip().lower() not in _FAIL_WORDS


# --------------------------------------------------------------------------- #
# jsonl — native fabri events (one file = one session, or split on a field)    #
# --------------------------------------------------------------------------- #
def jsonl_adapter(source: LogSource, options: dict) -> Iterator[Session]:
    """Records are already fabri events. If a ``session_key`` field is present
    (``options['session_key']`` or a ``session_id`` field on the events), split
    into one session per key; otherwise the whole file is one session."""
    session_key = options.get("session_key") or "session_id"
    records = list(source.records())
    have_key = any(_dotted(r, session_key) is not None for r in records)
    if have_key:
        buckets: dict[str, list[dict]] = {}
        order: list[str] = []
        for r in records:
            k = str(_dotted(r, session_key) or "_ungrouped")
            if k not in buckets:
                buckets[k] = []
                order.append(k)
            buckets[k].append(r)
        for k in order:
            yield Session(safe_session_id("jsonl", k), buckets[k])
    else:
        sid = safe_session_id("jsonl", _fingerprint(source.name))
        yield Session(sid, records)


# --------------------------------------------------------------------------- #
# regex — generic plaintext                                                    #
# --------------------------------------------------------------------------- #
def regex_adapter(source: LogSource, options: dict) -> Iterator[Session]:
    """``options['pattern']`` is a regex with named groups. Recognized groups:
    ``tool`` (call name), ``status`` (ok unless it reads as a failure word),
    ``error`` (message), ``task`` (sets the session task), ``session`` (grouping
    key). Lines that don't match are ignored. No ``session`` group → whole file
    is one session."""
    pattern = options.get("pattern")
    if not pattern:
        raise ValueError("regex adapter requires adapter_options['pattern'] (a regex with named groups)")
    rx = re.compile(pattern)

    # (session_key) -> {"task": str, "events": [...]}
    sessions: dict[str, dict] = {}
    order: list[str] = []
    for line in source.lines():
        m = rx.search(line)
        if not m:
            continue
        g = m.groupdict()
        skey = g.get("session") or "_all"
        if skey not in sessions:
            sessions[skey] = {"task": None, "events": []}
            order.append(skey)
        bucket = sessions[skey]
        if g.get("task") and not bucket["task"]:
            bucket["task"] = g["task"]
        if g.get("tool"):
            ok = _ok_from_status(g.get("status"))
            bucket["events"].append(
                tool_event(g["tool"], ok=ok, error=g.get("error") if not ok else None)
            )

    for skey in order:
        b = sessions[skey]
        task = b["task"] or (skey if skey != "_all" else source.name)
        events = [start_event(task), *b["events"], final_event(_infer_outcome(b["events"]))]
        yield Session(safe_session_id("regex", skey if skey != "_all" else _fingerprint(source.name)), events)


# --------------------------------------------------------------------------- #
# otel / openai — structured JSON records                                      #
# --------------------------------------------------------------------------- #
def otel_adapter(source: LogSource, options: dict) -> Iterator[Session]:
    """Map OTel-span-ish or OpenAI-response-ish JSON records to tool_call
    events, grouped by trace/conversation id. Best-effort: a record is treated
    as a span with a ``name`` and a status, or as an OpenAI response with an
    ``id`` and an ``error``. Unknown shapes contribute nothing."""
    session_key = options.get("session_key")
    sessions: dict[str, dict] = {}
    order: list[str] = []
    for rec in source.records():
        skey, task, ev = _otel_event(rec, session_key)
        if skey not in sessions:
            sessions[skey] = {"task": None, "events": []}
            order.append(skey)
        b = sessions[skey]
        if task and not b["task"]:
            b["task"] = task
        if ev is not None:
            b["events"].append(ev)

    for skey in order:
        b = sessions[skey]
        events = [start_event(b["task"] or skey), *b["events"], final_event(_infer_outcome(b["events"]))]
        yield Session(safe_session_id("otel", skey), events)


def _otel_event(rec: dict, session_key: str | None):
    """Return ``(session_key, task_or_None, event_or_None)`` for one record."""
    skey = None
    if session_key:
        skey = _dotted(rec, session_key)
    skey = skey or rec.get("trace_id") or rec.get("traceId") or _dotted(rec, "context.trace_id")
    task = rec.get("input") or rec.get("prompt") or _dotted(rec, "attributes.input")

    name = rec.get("name") or rec.get("span_name") or _dotted(rec, "attributes.tool.name")
    if name:
        status = rec.get("status") or _dotted(rec, "status.code") or rec.get("status_code")
        ok = _ok_from_status(str(status) if status is not None else None)
        err = rec.get("error") or _dotted(rec, "status.message")
        ev = tool_event(str(name), ok=ok, error=str(err) if (err and not ok) else None)
        return str(skey or "otel"), task, ev

    # OpenAI-response shape: an id + optional error, no span name.
    if "id" in rec and ("choices" in rec or "error" in rec or "response" in rec):
        skey = skey or rec.get("id")
        err = rec.get("error")
        ev = tool_event("llm.completion", ok=err is None, error=str(err) if err else None)
        return str(skey or "openai"), task, ev

    return str(skey or "otel"), task, None


# --------------------------------------------------------------------------- #
def _infer_outcome(events: list[dict]) -> str:
    """A session whose last tool_call failed reads as a failed run; any tool
    calls with all-ok reads as success; no tool calls → incomplete."""
    calls = [e for e in events if e.get("type") == EventType.TOOL_CALL.value]
    if not calls:
        return "incomplete"
    last = calls[-1]
    return "success" if (last.get("result") or {}).get("ok") else "failed"


def _fingerprint(name: str) -> str:
    """Short stable id for a whole-file session — avoids importing hashlib for a
    non-security use; just needs to be deterministic per source name."""
    import hashlib

    return hashlib.sha1(name.encode()).hexdigest()[:12]
