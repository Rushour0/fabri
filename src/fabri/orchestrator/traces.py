import fcntl
import json
import re
import time
from pathlib import Path

from fabri.core.logging_setup import get_logger
from fabri.paths import traces_dir

logger = get_logger()

# Session ids are framework-generated uuid4s (plus optional `_suffix`); anything
# outside this charset is rejected so a `session_id` like `../../etc/passwd`
# can't escape the traces dir on the read/replay/ingest paths a host may feed
# externally-supplied ids into.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def trace_path(session_id: str) -> Path:
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session_id {session_id!r}: must match {_SESSION_ID_RE.pattern}")
    return traces_dir() / f"{session_id}.jsonl"


def log_event(session_id: str, event: dict) -> None:
    record = {"ts": time.time(), **event}
    line = json.dumps(record) + "\n"
    with trace_path(session_id).open("a") as f:
        # Exclusive lock so concurrent appenders (parent + sub-agent both writing
        # to the same session id, or two sub-agents racing) can't interleave
        # mid-record. fcntl.flock is released when the fd is closed.
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)


def read_trace(session_id: str) -> list[dict]:
    """One malformed JSONL line shouldn't kill downstream processing of every
    other event in the trace (the pipeline mines traces for tool failures, etc.).
    Skip + log bad lines instead of raising."""
    path = trace_path(session_id)
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            logger.warning("trace %s: skipping malformed line %d: %s", session_id, i, e)
    return out
