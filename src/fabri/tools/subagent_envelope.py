"""The output contract a spawned sub-agent hands back to its parent.

A child's whole transcript stays in its own trace. Through ``spawn_subagent``
the parent gets a short envelope: a summary capped at a token budget, the
outcome, and the ``session_id`` + ``trace_path`` it needs to read the rest.

The budget comes from the parent's config (``tools.spawn_summary_max_tokens``)
and is applied on the parent's side, in ``spawn_subagent.py``. Static
``tools.agents[]`` children are not capped.

Rules:

- A prose ``final_text`` over budget is cut and gets a note pointing at the
  trace.
- When the child config declares ``agent.response_schema`` (the parent asked
  for typed output), a ``final_text`` that parses as JSON and the
  ``structured_output`` are kept whole if they validate against that schema:
  the parent gets all of it or a loud error. The spawn reads the schema
  before the child starts, so a child cannot add one to its own config.
  Without a schema, or for output that does not validate, JSON gets the same
  budget as prose; an over-budget value is replaced by
  ``{"truncated": true, "text": "<cut JSON text>"}`` so it stays valid JSON.
- ``usage`` and ``memory_actions_applied`` are bounded too, since they ride in
  the same tool result.
- If the envelope is still larger than :data:`HARD_MAX_BYTES` after that, it
  raises :class:`EnvelopeTooLarge`; the spawn turns that into an error that
  still carries ``outcome`` and bounded ``usage`` so the parent records cost.
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


def bounded_usage(usage: object) -> object:
    """``usage`` as is when small, else just its token and cost fields."""
    if usage is None or _size(usage) <= SIDE_FIELD_MAX_BYTES:
        return usage
    if not isinstance(usage, dict):
        return None
    return {k: usage[k] for k in _USAGE_KEYS if k in usage and _size(usage[k]) < 256}


def _bound_side_fields(out: dict) -> bool:
    cut = False
    usage = out.get("usage")
    if usage is not None and _size(usage) > SIDE_FIELD_MAX_BYTES:
        out["usage"] = bounded_usage(usage)
        cut = True
    actions = out.get("memory_actions_applied")
    if actions is not None and _size(actions) > SIDE_FIELD_MAX_BYTES:
        out["memory_actions_applied"] = []
        out["memory_actions_applied_dropped"] = len(actions) if isinstance(actions, list) else 1
        cut = True
    return cut


def _cut_json(text: str, max_tokens: int) -> tuple[str, int] | None:
    """An over-budget JSON text as ``{"truncated": true, "text": <cut>}``."""
    capped = _cap_text(text, max_tokens)
    if capped is None:
        return None
    return json.dumps({"truncated": True, "text": capped[0]}), capped[1]


def _matches(value: object, schema: dict | None) -> bool:
    if not isinstance(schema, dict) or not schema:
        return False
    from fabri.core.structured import validate

    try:
        return not validate(value, schema)
    except Exception:  # noqa: BLE001 -- a validator error is "does not match"
        return False


def cap_envelope(envelope: dict, max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
                 *, schema: dict | None = None, hard_max_bytes: int = HARD_MAX_BYTES) -> dict:
    """A copy of ``envelope`` with ``final_text`` and ``structured_output`` held
    to ``max_tokens`` (0 means no cut) and side fields bounded. ``schema`` is
    the child's response schema, read by the caller before the child ran: a
    JSON ``final_text`` or a ``structured_output`` that validates against it
    is kept whole; one that does not gets the normal cap. Always sets
    ``summary_truncated``. Raises :class:`EnvelopeTooLarge` when what is left
    is over ``hard_max_bytes``."""
    max_tokens = check_summary_cap(max_tokens)
    out = dict(envelope)
    truncated = False
    text = out.get("final_text")
    if max_tokens and isinstance(text, str):
        if _is_json(text):
            capped = None if _matches(json.loads(text), schema) else _cut_json(text, max_tokens)
            if capped is not None:
                out["final_text"], out["final_text_tokens"] = capped
                truncated = True
        else:
            capped = _cap_text(text, max_tokens)
            if capped is not None:
                out["final_text"] = capped[0] + TRUNCATION_NOTE
                out["final_text_tokens"] = capped[1]
                truncated = True
    structured = out.get("structured_output")
    if max_tokens and structured is not None and not _matches(structured, schema):
        try:
            dumped = json.dumps(structured)
        except (TypeError, ValueError):
            dumped = str(structured)
        capped = _cut_json(dumped, max_tokens)
        if capped is not None:
            out["structured_output"] = json.loads(capped[0])
            out["structured_output_tokens"] = capped[1]
            truncated = True
    truncated = _bound_side_fields(out) or truncated
    out["summary_truncated"] = truncated
    size = _size(out)
    if size > hard_max_bytes:
        raise EnvelopeTooLarge(
            f"sub-agent result is {size} bytes after capping the summary (limit {hard_max_bytes}); "
            "its structured or JSON output is too large to return whole")
    return out
