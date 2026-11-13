"""The output contract a spawned sub-agent hands back to its parent.

A child's whole transcript stays in its own trace. Through ``spawn_subagent``
the parent gets a short envelope: a summary capped at a token budget, the
outcome, and the ``session_id`` + ``trace_path`` it needs to read the rest.

The budget comes from the parent's config (``tools.spawn_summary_max_tokens``)
and is applied on the parent's side, in ``spawn_subagent.py``. Static
``tools.agents[]`` children are not capped.

Rules:

- Only a prose ``final_text`` is cut. A ``final_text`` that parses as JSON is
  kept whole, and ``structured_output`` is never dropped: a parent that asked
  for typed output gets all of it or a loud error.
- ``usage`` and ``memory_actions_applied`` are bounded too, since they ride in
  the same tool result.
- If the envelope is still larger than :data:`HARD_MAX_BYTES` after that, it
  raises :class:`EnvelopeTooLarge` instead of cutting anything structured.
- ``summary_truncated`` is always set, so a reader can tell "not cut" from
  "never checked".
"""
from __future__ import annotations

import json

DEFAULT_SUMMARY_MAX_TOKENS = 2000
# Under the runner's 1 MiB stdout cap with room for the error wrapper.
HARD_MAX_BYTES = 900 * 1024
# usage / memory_actions_applied above this size (JSON bytes) are reduced.
SIDE_FIELD_MAX_BYTES = 8 * 1024
TRUNCATION_NOTE = "\n\n[summary truncated; full text is in the child trace at trace_path]"
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
               "cost_usd", "total_cost_usd", "model")


class EnvelopeTooLarge(ValueError):
    """The envelope cannot fit the hard cap without cutting structured data."""


class SummaryCapError(ValueError):
    """A summary budget that is not a non-negative integer."""


def check_summary_cap(value: object) -> int:
    """Validate a summary budget. Accepts a non-negative int (bools refused)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryCapError(f"summary cap must be a non-negative integer, got {value!r}")
    return value


def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 -- no tokenizer: fall back to ~4 chars/token
        return None


def count_tokens(text: str) -> int:
    """Token count of ``text`` (cl100k), ~4 chars/token without tiktoken.
    Special-token strings are counted as plain text, never raised on."""
    enc = _encoding()
    if enc is None:
        return (len(text) + 3) // 4
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001 -- e.g. lone surrogates
        return (len(text) + 3) // 4


def _cap_text(text: str, max_tokens: int) -> tuple[str, int] | None:
    """(cut text, original token estimate) when ``text`` is over budget, else
    None. Very long input is sliced by characters first so the tokenizer
    never sees megabytes."""
    if len(text) <= max_tokens:  # every token is at least one character
        return None
    window = text[: max_tokens * 8]
    enc = _encoding()
    try:
        tokens = enc.encode(window, disallowed_special=()) if enc else None
    except Exception:  # noqa: BLE001
        tokens = None
    if tokens is None:
        if len(text) <= max_tokens * 4:
            return None
        return text[: max_tokens * 4], (len(text) + 3) // 4
    if len(window) == len(text) and len(tokens) <= max_tokens:
        return None
    original = len(tokens) if len(window) == len(text) else max(len(tokens), (len(text) + 3) // 4)
    cut = enc.decode(tokens[:max_tokens])
    if " " in cut.rstrip():
        cut = cut.rstrip().rsplit(" ", 1)[0]
    return cut, original


def _is_json(text: str) -> bool:
    s = text.strip()
    if not s or s[0] not in "{[":
        return False
    try:
        json.loads(s)
    except (ValueError, RecursionError):
        return False
    return True


def _size(value: object) -> int:
    try:
        return len(json.dumps(value).encode("utf-8", "replace"))
    except (TypeError, ValueError):
        return SIDE_FIELD_MAX_BYTES + 1


def _bound_side_fields(out: dict) -> bool:
    cut = False
    usage = out.get("usage")
    if usage is not None and _size(usage) > SIDE_FIELD_MAX_BYTES:
        out["usage"] = ({k: usage[k] for k in _USAGE_KEYS if k in usage and _size(usage[k]) < 256}
                        if isinstance(usage, dict) else None)
        cut = True
    actions = out.get("memory_actions_applied")
    if actions is not None and _size(actions) > SIDE_FIELD_MAX_BYTES:
        out["memory_actions_applied"] = []
        out["memory_actions_applied_dropped"] = len(actions) if isinstance(actions, list) else 1
        cut = True
    return cut


def cap_envelope(envelope: dict, max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
                 *, hard_max_bytes: int = HARD_MAX_BYTES) -> dict:
    """A copy of ``envelope`` with a prose ``final_text`` cut to ``max_tokens``
    (0 means no cut) and side fields bounded. Always sets
    ``summary_truncated``. Raises :class:`EnvelopeTooLarge` when what is left
    is over ``hard_max_bytes``."""
    max_tokens = check_summary_cap(max_tokens)
    out = dict(envelope)
    truncated = False
    text = out.get("final_text")
    if max_tokens and isinstance(text, str) and not _is_json(text):
        capped = _cap_text(text, max_tokens)
        if capped is not None:
            out["final_text"] = capped[0] + TRUNCATION_NOTE
            out["final_text_tokens"] = capped[1]
            truncated = True
    truncated = _bound_side_fields(out) or truncated
    out["summary_truncated"] = truncated
    size = _size(out)
    if size > hard_max_bytes:
        raise EnvelopeTooLarge(
            f"sub-agent result is {size} bytes after capping the summary (limit {hard_max_bytes}); "
            "its structured or JSON output is too large to return whole")
    return out
