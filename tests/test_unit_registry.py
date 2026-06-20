"""Unit tests for ToolRegistry: discovery, programmatic registration, and
the override-by-name semantics that let a project's tool dir shadow a
framework-default of the same name."""
import json
from pathlib import Path

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ToolRegistry


def _make_tool_dir(root: Path, names: list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / f"{n}.json").write_text(json.dumps({
            "name": n, "description": n, "command": ["true"],
        }))
    return root


def test_discovers_all_json_in_dir(tmp_path):
    d = _make_tool_dir(tmp_path / "a", ["one", "two", "three"])
    reg = ToolRegistry(d)
    assert set(reg.tools) == {"one", "two", "three"}


def test_accepts_single_path_or_list(tmp_path):
    d1 = _make_tool_dir(tmp_path / "a", ["x"])
    d2 = _make_tool_dir(tmp_path / "b", ["y"])
    assert set(ToolRegistry(d1).tools) == {"x"}
    assert set(ToolRegistry([d1, d2]).tools) == {"x", "y"}


def test_later_dir_overrides_earlier_on_name_collision(tmp_path):
    # A project's tools/ dir comes second in the manifest_dir list and should
    # win over a same-named framework default, by deliberate dict-update order.
    framework = tmp_path / "framework"
    project = tmp_path / "project"
    framework.mkdir(); project.mkdir()
    (framework / "shared.json").write_text(json.dumps({
        "name": "shared", "description": "framework", "command": ["true"],
    }))
    (project / "shared.json").write_text(json.dumps({
        "name": "shared", "description": "project", "command": ["true"],
    }))
    reg = ToolRegistry([framework, project])
    assert reg.tools["shared"].description == "project"


def test_register_adds_programmatic_manifest(tmp_path):
    reg = ToolRegistry(_make_tool_dir(tmp_path / "a", ["x"]))
    m = ToolManifest(name="y", description="d", command=["true"], input_schema={}, output_schema={})
    reg.register(m)
    assert set(reg.tools) == {"x", "y"}


def test_register_overrides_existing_name(tmp_path):
    reg = ToolRegistry(_make_tool_dir(tmp_path / "a", ["x"]))
    m = ToolManifest(name="x", description="replaced", command=["true"], input_schema={}, output_schema={})
    reg.register(m)
    assert reg.tools["x"].description == "replaced"


def test_list_returns_all_registered():
    reg = ToolRegistry([])
    m1 = ToolManifest(name="a", description="d", command=["true"], input_schema={}, output_schema={})
    m2 = ToolManifest(name="b", description="d", command=["true"], input_schema={}, output_schema={})
    reg.register(m1); reg.register(m2)
    assert {t.name for t in reg.list()} == {"a", "b"}


def test_invoke_unknown_returns_normalized_error():
    reg = ToolRegistry([])
    assert reg.invoke("nope", {}) == {"ok": False, "error": "unknown tool: nope"}


def test_empty_dir_list_is_valid(tmp_path):
    reg = ToolRegistry([])
    assert reg.tools == {}
    assert reg.list() == []
