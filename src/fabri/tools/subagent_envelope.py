"""The output contract a sub-agent hands back to its parent.

A child's whole transcript stays in its own trace. The parent gets a short
envelope: a summary capped at a token budget, the outcome, and the
``session_id`` + ``trace_path`` it needs to read the rest. The cap is applied
to the summary field only, so the envelope is always valid JSON. The byte cap
in ``fabri.tools.runner`` is a last-resort ceiling for any tool and cuts raw
stdout, which breaks JSON; this cap runs first and keeps the envelope whole.
"""
from __future__ import annotations

import json

DEFAULT_SUMMARY_MAX_TOKENS = 2000
TRUNCATION_NOTE = "\n\n[summary truncated; full text is in the child trace at trace_path]"


def _count_tokens(text: str) -> int:
    try:
        from fabri.memory.compress import count_tokens

        return count_tokens(text)
    except Exception:  # noqa: BLE001 -- tokenizer missing: fall back to ~4 chars/token
        return (len(text) + 3) // 4


def _cap_tokens(text: str, max_tokens: int) -> str:
    try:
        from fabri.memory.compress import enforce_token_cap

        return enforce_token_cap(text, max_tokens)
    except Exception:  # noqa: BLE001
        return text[: max_tokens * 4]


def cap_envelope(envelope: dict, max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS) -> dict:
    """Return a copy of ``envelope`` whose ``final_text`` fits ``max_tokens``
    and whose ``structured_output`` is dropped when it alone exceeds the
    budget. Adds ``summary_truncated`` and, when cut, ``final_text_tokens``
    (the original size). Every other key passes through unchanged.
    ``max_tokens <= 0`` disables the cap."""
    out = dict(envelope)
    if max_tokens <= 0:
        return out
    text = out.get("final_text")
    truncated = False
    if isinstance(text, str):
        n = _count_tokens(text)
        if n > max_tokens:
            out["final_text"] = _cap_tokens(text, max_tokens) + TRUNCATION_NOTE
            out["final_text_tokens"] = n
            truncated = True
    structured = out.get("structured_output")
    if structured is not None:
        try:
            size = _count_tokens(json.dumps(structured))
        except (TypeError, ValueError):
            size = max_tokens + 1
        if size > max_tokens:
            out["structured_output"] = None
            out["structured_output_dropped_tokens"] = size
            truncated = True
    out["summary_truncated"] = truncated
    return out
