"""B2 -- tool-writer: signature->schema parsing, manifest validation, and an
end-to-end new -> validate -> test round-trip on a generated tool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabri.builder import new_tool, parse_signature, validate_manifest
from fabri.builder import test_tool as run_tool_local  # aliased: pytest collects `test_*`
from fabri.builder.tool_writer import check_schema


# ---------------------------------------------------------------------------
# parse_signature: typed params -> input_schema, return -> output_schema
# ---------------------------------------------------------------------------

_SIG_SOURCE = '''\
def add_numbers(a: int, b: int, label: str = "sum", scale: float = 1.0) -> int:
    """Add two integers and return the scaled result."""
    return int((a + b) * scale)
'''


def _write_sig(tmp_path: Path, src: str = _SIG_SOURCE) -> Path:
    f = tmp_path / "sig.py"
    f.write_text(src)
    return f


def test_parse_signature_maps_types_and_required(tmp_path):
    spec = parse_signature(_write_sig(tmp_path))
    assert spec["func_name"] == "add_numbers"
    props = spec["input_schema"]["properties"]
    assert props["a"] == {"type": "integer"}
    assert props["b"] == {"type": "integer"}
    assert props["label"] == {"type": "string"}
    assert props["scale"] == {"type": "number"}
    # required = params without a default
    assert spec["input_schema"]["required"] == ["a", "b"]
    # return annotation lands on output_schema.result
    assert spec["output_schema"]["properties"]["result"] == {"type": "integer"}


def test_parse_signature_container_and_optional_types(tmp_path):
    src = (
        "from typing import Optional, List\n"
        "def f(items: list, mapping: dict, maybe: Optional[int], tags: List[str]):\n"
        "    return {'n': len(items)}\n"
    )
    spec = parse_signature(_write_sig(tmp_path, src))
    props = spec["input_schema"]["properties"]
    assert props["items"] == {"type": "array"}
    assert props["mapping"] == {"type": "object"}
    assert props["maybe"] == {"type": "integer"}  # Optional unwrapped
    assert props["tags"] == {"type": "array"}
    # no return annotation -> result is untyped (no assertion)
    assert spec["output_schema"]["properties"]["result"] == {}


def test_parse_signature_unannotated_param_is_untyped(tmp_path):
    spec = parse_signature(_write_sig(tmp_path, "def g(x):\n    return x\n"))
    assert spec["input_schema"]["properties"]["x"] == {}
    assert spec["input_schema"]["required"] == ["x"]


def test_parse_signature_no_function_raises(tmp_path):
    f = tmp_path / "empty.py"
    f.write_text("x = 1\n")
    with pytest.raises(ValueError, match="no top-level function"):
        parse_signature(f)


# ---------------------------------------------------------------------------
# check_schema / validate_manifest: pass and fail paths
# ---------------------------------------------------------------------------


def test_check_schema_accepts_valid_subset():
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "mode": {"enum": ["a", "b"]},
        },
        "required": ["n"],
    }
    assert check_schema(schema) == []


def test_check_schema_rejects_bad_type_token():
    errors = check_schema({"type": "strng"})
    assert errors and "not a supported type" in errors[0]


def test_validate_manifest_pass(tmp_path):
    (tmp_path / "echo.py").write_text("print('{}')\n")
    manifest = {
        "name": "echo",
        "description": "Echo back the args.",
        "command": ["python3", "echo.py"],
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "timeout_s": 10,
    }
    path = tmp_path / "echo.json"
    path.write_text(json.dumps(manifest))
    ok, lines = validate_manifest(path)
    assert ok, "\n".join(lines)
    assert lines[-1] == "PASS"


def test_validate_manifest_fails_on_missing_script(tmp_path):
    manifest = {
        "name": "ghost",
        "description": "Points at a script that doesn't exist.",
        "command": ["python3", "ghost.py"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }
    path = tmp_path / "ghost.json"
    path.write_text(json.dumps(manifest))
    ok, lines = validate_manifest(path)
    assert not ok
    assert any("script not found" in line for line in lines)


def test_validate_manifest_fails_on_bad_schema(tmp_path):
    (tmp_path / "t.py").write_text("print('{}')\n")
    manifest = {
        "name": "t",
        "description": "Bad schema type token.",
        "command": ["python3", "t.py"],
        "input_schema": {"type": "nope"},
        "output_schema": {"type": "object"},
    }
    path = tmp_path / "t.json"
    path.write_text(json.dumps(manifest))
    ok, lines = validate_manifest(path)
    assert not ok
    assert any("not a supported type" in line for line in lines)


def test_validate_manifest_missing_file(tmp_path):
    ok, lines = validate_manifest(tmp_path / "nope.json")
    assert not ok
    assert "not found" in lines[0]


# ---------------------------------------------------------------------------
# new_tool: scaffold modes
# ---------------------------------------------------------------------------


def test_new_tool_default_uses_tightened_schema(tmp_path):
    result = new_tool("widget", target_dir=tmp_path)
    assert result["mode"] == "default"
    manifest = json.loads((tmp_path / "widget.json").read_text())
    # Tightened: an object schema with a properties slot, not an opaque {}.
    assert manifest["input_schema"] == {"type": "object", "properties": {}}


def test_new_tool_from_desc_without_llm_falls_back(tmp_path):
    result = new_tool("describer", from_desc="Summarize text.", target_dir=tmp_path, llm=None)
    assert result["mode"] == "from-desc"
    manifest = json.loads((tmp_path / "describer.json").read_text())
    assert manifest["description"] == "Summarize text."
    assert manifest["input_schema"] == {"type": "object", "properties": {}}


def test_new_tool_from_desc_with_scripted_llm(tmp_path):
    from fabri.core.llm import LLMResponse, ScriptedLLMBackend

    reply = json.dumps({
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
    })
    llm = ScriptedLLMBackend([LLMResponse(final_text=reply)])
    new_tool("summarizer", from_desc="Summarize text.", target_dir=tmp_path, llm=llm)
    manifest = json.loads((tmp_path / "summarizer.json").read_text())
    assert manifest["input_schema"]["required"] == ["text"]
    assert manifest["output_schema"]["properties"]["summary"] == {"type": "string"}


def test_new_tool_from_desc_with_bad_llm_reply_falls_back(tmp_path):
    from fabri.core.llm import LLMResponse, ScriptedLLMBackend

    llm = ScriptedLLMBackend([LLMResponse(final_text="not json at all")])
    new_tool("fallbacker", from_desc="Do a thing.", target_dir=tmp_path, llm=llm)
    manifest = json.loads((tmp_path / "fallbacker.json").read_text())
    assert manifest["input_schema"] == {"type": "object", "properties": {}}


def test_new_tool_from_signature_rejects_non_python(tmp_path):
    sig = _write_sig(tmp_path)
    with pytest.raises(ValueError, match="only supports --lang python"):
        new_tool("x", lang="go", from_signature=str(sig), target_dir=tmp_path)


# ---------------------------------------------------------------------------
# End-to-end: new --from-signature -> validate -> test, all green
# ---------------------------------------------------------------------------


def test_signature_round_trip_new_validate_test(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sig = src_dir / "fn.py"
    sig.write_text(
        "def add_numbers(a: int, b: int) -> int:\n"
        '    """Add two integers."""\n'
        "    return a + b\n"
    )
    out_dir = tmp_path / "tools"

    # new
    result = new_tool("add_numbers", from_signature=str(sig), target_dir=out_dir)
    assert result["mode"] == "from-signature"
    assert "add_numbers.json" in result["created"]
    assert "add_numbers.py" in result["created"]

    # validate
    ok, lines = validate_manifest(out_dir / "add_numbers.json")
    assert ok, "\n".join(lines)

    # test (runs the generated stub through the real runner/sandbox)
    envelope = run_tool_local("add_numbers", {"a": 2, "b": 3}, target_dir=out_dir)
    assert envelope["ok"] is True, envelope
    assert envelope["result"] == {"result": 5}


def test_test_tool_unknown_manifest_raises(tmp_path):
    with pytest.raises(ValueError, match="no manifest"):
        run_tool_local("nope", {}, target_dir=tmp_path)
