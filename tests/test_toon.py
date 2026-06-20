"""Round-trip + shape tests for the vendored TOON codec. The hard guarantee the
rest of the framework leans on is decode(encode(x)) == x for any JSON value with
an object/array at the top level -- so a tool result encoded into the context
never loses or corrupts data."""
import random

import pytest

from fabri import toon


def _round_trips(value):
    assert toon.decode(toon.encode(value)) == value


# --- concrete shapes the framework actually serializes ---------------------- #


def test_flat_tool_result():
    _round_trips({"ok": True, "result": {"path": "note.txt", "bytes_written": 5}})


def test_text_blob_result_with_newlines():
    _round_trips({"ok": True, "result": {"path": "a.txt", "content": "hello\nworld\t!"}})


def test_uniform_object_array_uses_table_form():
    value = {"ok": True, "result": {"entries": [
        {"name": "a.txt", "is_dir": False},
        {"name": "sub", "is_dir": True},
    ]}}
    encoded = toon.encode(value)
    # the array collapses to one header row + N data rows, no repeated keys
    assert "entries[2]{name,is_dir}:" in encoded
    assert '"name"' not in encoded and '"is_dir"' not in encoded
    _round_trips(value)


def test_primitive_array_is_inline():
    value = {"tags": ["a", "b", "c"], "nums": [1, 2, 3]}
    encoded = toon.encode(value)
    assert "tags[3]: a,b,c" in encoded
    assert "nums[3]: 1,2,3" in encoded
    _round_trips(value)


def test_failure_result_shape():
    _round_trips({"ok": False, "error": "tool exited 1",
                  "result": {"stdout": "", "stderr": "boom: x,y"}, "stderr": "boom: x,y"})


def test_non_uniform_array_falls_back_to_list_form():
    _round_trips({"items": [{"a": 1}, {"b": 2, "c": 3}, "plain", [1, 2]]})


# --- ambiguity / quoting edges --------------------------------------------- #


@pytest.mark.parametrize("value", [
    {"s": ""},                       # empty string vs empty object
    {"s": "  padded  "},             # leading/trailing whitespace
    {"s": "123"},                    # numeric-looking string
    {"s": "true"}, {"s": "false"}, {"s": "null"},  # literal-looking strings
    {"s": "a,b,c"},                  # contains the delimiter
    {"s": '{"json": 1}'},            # starts with a structural marker
    {"s": "- dash"}, {"s": "[bracket"},
    {"empty_obj": {}},               # empty object
    {"empty_arr": []},               # empty array
    {"nested": {"deep": {"deeper": {"x": 1}}}},
    {"mixed": [1, "two", True, None, 3.5]},
    {"n": -3}, {"f": -2.5}, {"big": 10 ** 20},
    {"unicode": "café — résumé ✓"},
    {"key with spaces": 1, "k:colon": 2, '"quoted"': 3},
    [1, 2, 3],                       # top-level array
    [{"id": 1, "v": "x"}, {"id": 2, "v": "y"}],  # top-level table
])
def test_round_trip_edges(value):
    _round_trips(value)


def test_empty_object_and_array_roundtrip():
    assert toon.decode(toon.encode({})) == {}
    assert toon.decode(toon.encode([])) == []


def test_top_level_scalar_rejected():
    with pytest.raises(ValueError):
        toon.encode("just a string")


# --- fuzz: random JSON-shaped trees ---------------------------------------- #

_STRINGS = ["", "x", "hello world", "a,b", "true", "null", "123", "1.5",
            "- d", "[x", "{y", "café", "tab\tsep", "line\nbreak", 'q"q']


def _rand_value(rng, depth):
    if depth <= 0:
        return rng.choice([
            rng.randint(-1000, 1000), round(rng.uniform(-100, 100), 4),
            rng.choice([True, False, None]), rng.choice(_STRINGS),
        ])
    kind = rng.choice(["scalar", "scalar", "list", "dict"])
    if kind == "scalar":
        return _rand_value(rng, 0)
    if kind == "list":
        return [_rand_value(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    keys = rng.sample(["a", "b", "c", "d", "k:e", "f g", ""], rng.randint(0, 4))
    return {k: _rand_value(rng, depth - 1) for k in keys}


def test_fuzz_round_trip():
    rng = random.Random(20260619)
    for _ in range(400):
        value = rng.choice([
            {k: _rand_value(rng, 3) for k in rng.sample(["a", "b", "c", "d"], rng.randint(1, 4))},
            [_rand_value(rng, 3) for _ in range(rng.randint(0, 5))],
        ])
        assert toon.decode(toon.encode(value)) == value, value


def test_toon_is_fewer_chars_than_json_on_tables():
    import json
    value = {"rows": [{"id": i, "name": f"n{i}", "ok": True} for i in range(20)]}
    assert len(toon.encode(value)) < len(json.dumps(value))
