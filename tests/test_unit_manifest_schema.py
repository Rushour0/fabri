"""Unit tests for ToolManifest.from_file. The path-resolution logic
(rewriting command parts that name a sibling file into absolute paths) is
what lets a manifest_dir be added to a registry from any cwd -- worth
pinning explicitly."""
import json

import pytest

from fabri.tools.manifest_schema import ToolManifest


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_command_with_sibling_script_resolved_to_absolute(tmp_path):
    (tmp_path / "my_tool.py").write_text("print('x')")
    p = _write(tmp_path, "t.json", {
        "name": "t", "description": "d",
        "command": ["python3", "my_tool.py"],
    })
    m = ToolManifest.from_file(p)
    assert m.command[0] == "python3"
    assert m.command[1] == str((tmp_path / "my_tool.py").resolve())


def test_command_with_bare_executable_left_untouched(tmp_path):
    p = _write(tmp_path, "t.json", {
        "name": "t", "description": "d",
        "command": ["echo", "hello"],
    })
    m = ToolManifest.from_file(p)
    assert m.command == ["echo", "hello"]


def test_default_timeout(tmp_path):
    p = _write(tmp_path, "t.json", {
        "name": "t", "description": "d", "command": ["true"],
    })
    m = ToolManifest.from_file(p)
    assert m.timeout_s == 10.0


def test_explicit_timeout_preserved(tmp_path):
    p = _write(tmp_path, "t.json", {
        "name": "t", "description": "d", "command": ["true"], "timeout_s": 42,
    })
    m = ToolManifest.from_file(p)
    assert m.timeout_s == 42


def test_schemas_default_to_empty(tmp_path):
    p = _write(tmp_path, "t.json", {
        "name": "t", "description": "d", "command": ["true"],
    })
    m = ToolManifest.from_file(p)
    assert m.input_schema == {}
    assert m.output_schema == {}
