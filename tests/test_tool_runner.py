from pathlib import Path

from fabri.tools.registry import ToolRegistry

TOOLS_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


def make_registry() -> ToolRegistry:
    return ToolRegistry(TOOLS_DIR)


def test_python_tool_round_trip():
    registry = make_registry()
    result = registry.invoke("echo", {"hello": "world"})
    assert result == {"ok": True, "result": {"echoed": {"hello": "world"}}}


def test_go_tool_round_trip():
    registry = make_registry()
    result = registry.invoke("sum", {"a": 2, "b": 3})
    assert result["ok"] is True
    assert result["result"]["sum"] == 5


def test_malformed_output_is_a_failure_not_a_crash():
    registry = make_registry()
    result = registry.invoke("broken", {"trigger": "fast"})
    assert result["ok"] is False
    assert "malformed" in result["error"]


def test_timeout_is_a_distinct_failure_mode():
    registry = make_registry()
    result = registry.invoke("broken", {"trigger": "slow"})
    assert result["ok"] is False
    assert "timeout" in result["error"]


def test_unknown_tool_returns_normalized_error():
    registry = make_registry()
    result = registry.invoke("does-not-exist", {})
    assert result == {"ok": False, "error": "unknown tool: does-not-exist"}
