import json
import time
from pathlib import Path

from agent_memory.paths import traces_dir


def trace_path(session_id: str) -> Path:
    return traces_dir() / f"{session_id}.jsonl"


def log_event(session_id: str, event: dict) -> None:
    record = {"ts": time.time(), **event}
    with trace_path(session_id).open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_trace(session_id: str) -> list[dict]:
    path = trace_path(session_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
