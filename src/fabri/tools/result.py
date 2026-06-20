"""Canonical tool-response shape for every fabri tool.

The agent loop, the trace pipeline, and ludexel-side trace readers all key off
the `ok` boolean inside a tool's response. Before this module that boolean was
hand-written at ~10 sites; one typo (`"OK"` vs `True`) silently flipped a
failure into a success. The `ToolStatus` enum + `tool_ok` / `tool_error`
factory functions concentrate the shape in one place so the rest of fabri can
say `return tool_ok(parsed)` / `return tool_error("path escapes sandbox")`.

The wire shape is unchanged: `{"ok": bool, "result": dict | None, "error": str
| None}`. Existing consumers reading `result["ok"]` keep working; the enum is
strictly an internal authoring aid. We expose `STATUS_KEY` + `OK_KEY` for the
~1 reader that wants to be explicit.
"""
from enum import Enum
from typing import Any


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


OK_KEY = "ok"
RESULT_KEY = "result"
ERROR_KEY = "error"


def tool_ok(result: dict | None = None, **extra: Any) -> dict:
    """Successful tool response. `result` is the tool's structured payload
    (the thing the model will read). `**extra` is for runner-side metadata
    like `stderr_tail` that the runner stitches on top."""
    out: dict = {OK_KEY: True}
    if result is not None:
        out[RESULT_KEY] = result
    out.update(extra)
    return out


def tool_error(error: str, result: dict | None = None, **extra: Any) -> dict:
    """Failed tool response. `error` is the short human-readable cause that
    the trace pipeline mines into guidelines; `result` carries any partial
    structured payload the tool managed to produce before failing (the
    subprocess runner stitches the tool's own JSON in here even on non-zero
    exit so we don't lose its diagnostics)."""
    out: dict = {OK_KEY: False, ERROR_KEY: error}
    if result is not None:
        out[RESULT_KEY] = result
    out.update(extra)
    return out


def is_ok(response: dict) -> bool:
    return bool(response.get(OK_KEY, False))


def is_error(response: dict) -> bool:
    return not is_ok(response)
