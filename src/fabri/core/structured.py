"""O1: structured / typed output.

Validate an agent's final answer against an optional JSON Schema
(`agent.response_schema`). The agent loop uses `parse_response` to turn the
model's final text into a validated object; on a mismatch it re-prompts the
model with the human-readable errors (up to a bounded retry count) before
resolving per `agent.error_strategy`.

We ship a small, dependency-free validator covering the JSON-Schema subset that
actually matters for LLM structured output: ``type`` (incl. a list of types),
``properties``, ``required``, ``items``, and ``enum``, recursing through nested
objects and arrays. It is intentionally NOT a full Draft-2020 implementation --
no ``$ref`` / ``allOf`` / ``pattern`` / numeric bounds / format assertions. The
question it answers is "did the model return the shape you asked for", not RFC
compliance. Unknown keywords are ignored rather than erroring, so a richer
schema still validates on the subset it understands.
"""

import json

from fabri.core.decompose import _strip_fences

# JSON-Schema `type` token -> predicate. bool is excluded from the numeric
# checks because `isinstance(True, int)` is True in Python and a model that
# returns `true` for an integer field is almost certainly wrong.
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class StructuredOutputError(ValueError):
    """The model's final answer could not be parsed as JSON or did not match
    `agent.response_schema` after the configured retries, under the `strict`
    error strategy. The agent loop maps this to Outcome.INVALID_OUTPUT and ends
    the run cleanly -- a host can branch on the outcome instead of scraping a
    free-text answer that doesn't fit the contract."""


def parse_response(text: str, schema: dict) -> tuple[object, list[str]]:
    """Parse `text` (the model's final answer) as JSON and validate it against
    `schema`. Returns ``(value, errors)``: `value` is the decoded object (or
    None if it wasn't even valid JSON) and `errors` is a list of human-readable
    problems (empty == valid). Markdown code fences are stripped first, mirroring
    `decompose` -- models wrap JSON in ```` ```json ```` even when told not to.
    """
    cleaned = _strip_fences(text or "")
    try:
        value = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        return None, [f"response is not valid JSON: {e}"]
    return value, validate(value, schema)


def validate(value: object, schema: dict, path: str = "$") -> list[str]:
    """Validate `value` against the supported JSON-Schema subset. Returns a list
    of human-readable error strings (empty == valid). Pure; no I/O."""
    if not isinstance(schema, dict):
        return []  # nothing to assert against
    errors: list[str] = []

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types):
            return [f"{path}: expected type {expected!r}, got {_typename(value)}"]

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: {value!r} is not one of {enum!r}")

    if isinstance(value, dict):
        for key in schema.get("required", []) or []:
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        props = schema.get("properties") or {}
        for key, subschema in props.items():
            if key in value:
                errors.extend(validate(value[key], subschema, f"{path}.{key}"))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, element in enumerate(value):
                errors.extend(validate(element, items, f"{path}[{i}]"))

    return errors


def _typename(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__
