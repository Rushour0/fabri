"""A3: batch tool. Dispatching N tool calls inside one model turn collapses
N round-trips of full prompt + accumulated tool_result history to one. The
batch is intentionally in-process: it recurses into the registry's own
`invoke()` rather than spawning a subprocess.
"""
import os
from pathlib import Path

import pytest

from fabri.tools.registry import BATCH_TOOL_NAME, ToolRegistry

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


def _registry(tmp_path: Path) -> ToolRegistry:
    os.environ["FABRI_SANDBOX_ROOT"] = str(tmp_path)
    return ToolRegistry(EXAMPLES_DIR, sandbox_root=str(tmp_path))


def test_batch_dispatches_each_call_and_collects_results(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    reg = _registry(tmp_path)
    assert BATCH_TOOL_NAME in reg.tools

    result = reg.invoke(BATCH_TOOL_NAME, {"calls": [
        {"name": "read_file", "args": {"path": "a.txt"}},
        {"name": "read_file", "args": {"path": "b.txt"}},
    ]})
    assert result["ok"] is True
    results = result["result"]["results"]
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert results[0]["result"]["content"] == "alpha"
    assert results[1]["result"]["content"] == "beta"


def test_batch_refuses_nested_batch(tmp_path):
    reg = _registry(tmp_path)
    result = reg.invoke(BATCH_TOOL_NAME, {"calls": [
        {"name": "batch", "args": {"calls": []}},
    ]})
    # The outer batch itself succeeds; the nested call returns an error result
    # so the model sees a clear "flatten this" signal rather than a silent skip.
    assert result["ok"] is True
    inner = result["result"]["results"][0]
    assert inner["ok"] is False
    assert "nested batch" in inner["error"]


def test_batch_refuses_side_effecting_meta_tools(tmp_path):
    reg = _registry(tmp_path)
    result = reg.invoke(BATCH_TOOL_NAME, {"calls": [
        {"name": "spawn_subagent", "args": {}},
        {"name": "ask_user", "args": {}},
    ]})
    assert result["ok"] is True
    for inner in result["result"]["results"]:
        assert inner["ok"] is False


def test_batch_per_call_failure_does_not_short_circuit(tmp_path):
    (tmp_path / "ok.txt").write_text("ok")
    reg = _registry(tmp_path)
    result = reg.invoke(BATCH_TOOL_NAME, {"calls": [
        {"name": "read_file", "args": {"path": "missing.txt"}},
        {"name": "read_file", "args": {"path": "ok.txt"}},
    ]})
    results = result["result"]["results"]
    assert results[0]["ok"] is False
    assert results[1]["ok"] is True
    assert results[1]["result"]["content"] == "ok"


def test_batch_malformed_calls_returns_error(tmp_path):
    reg = _registry(tmp_path)
    result = reg.invoke(BATCH_TOOL_NAME, {"calls": "not a list"})
    assert result["ok"] is False


def test_batch_malformed_entry_returns_per_entry_error(tmp_path):
    reg = _registry(tmp_path)
    result = reg.invoke(BATCH_TOOL_NAME, {"calls": [{"no_name_field": "x"}]})
    assert result["ok"] is True
    assert result["result"]["results"][0]["ok"] is False
