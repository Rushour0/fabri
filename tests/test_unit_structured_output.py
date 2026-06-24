"""Unit tests for the dependency-free structured-output validator (O1).
Pure functions -- no LLM, no store, no Qdrant. Covers the supported JSON-Schema
subset (type / properties / required / items / enum), fence stripping, and the
parse-then-validate entry point used by the agent loop."""

from fabri.core.structured import parse_response, validate


def test_valid_object_passes():
    schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    assert validate({"name": "Ada", "age": 36}, schema) == []


def test_missing_required_key_reported():
    schema = {"type": "object", "required": ["name", "age"], "properties": {}}
    errors = validate({"name": "Ada"}, schema)
    assert any("missing required property 'age'" in e for e in errors)


def test_wrong_top_level_type():
    errors = validate([1, 2, 3], {"type": "object"})
    assert len(errors) == 1 and "expected type 'object'" in errors[0]


def test_bool_is_not_integer():
    # isinstance(True, int) is True in Python; the validator must reject it so a
    # model returning `true` for an int field is flagged, not silently accepted.
    errors = validate({"n": True}, {"type": "object", "properties": {"n": {"type": "integer"}}})
    assert any("$.n" in e for e in errors)


def test_number_accepts_int_and_float():
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    assert validate({"x": 3}, schema) == []
    assert validate({"x": 3.5}, schema) == []


def test_enum_enforced():
    schema = {"type": "string", "enum": ["red", "green", "blue"]}
    assert validate("green", schema) == []
    assert validate("purple", schema)


def test_nested_array_items_validated():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert validate({"tags": ["a", "b"]}, schema) == []
    errors = validate({"tags": ["a", 2]}, schema)
    assert any("tags[1]" in e for e in errors)


def test_type_list_allows_either():
    schema = {"type": ["string", "null"]}
    assert validate(None, schema) == []
    assert validate("x", schema) == []
    assert validate(5, schema)


def test_unknown_keywords_ignored():
    # A richer schema still validates on the subset we understand; pattern is
    # not asserted, so a non-matching string is NOT rejected for pattern.
    schema = {"type": "string", "pattern": "^[0-9]+$", "minLength": 100}
    assert validate("abc", schema) == []


def test_parse_response_strips_code_fences():
    value, errors = parse_response('```json\n{"ok": true}\n```', {"type": "object"})
    assert errors == [] and value == {"ok": True}


def test_parse_response_non_json_reported():
    value, errors = parse_response("this is not json", {"type": "object"})
    assert value is None
    assert errors and "not valid JSON" in errors[0]
