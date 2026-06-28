"""B3 -- runner & discovery ergonomics.

`fabri tools` must list the registry's tools (and filter by --search) the same
way a run resolves them, and `fabri run --dry-run` must print the resolved
config + the tool defs that would be sent to the model WITHOUT opening the
memory store, building an LLM backend, or requiring an API key. These tests pin
both, and assert the dry-run path never touches the network.
"""
import argparse
import sys

import pytest

from fabri import cli
from fabri.builder import (
    build_dry_run_plan,
    filter_tools,
    render_dry_run_plan,
    render_tools_listing,
)
from fabri.config import DEFAULT_TOOLS_DIR
from fabri.tools.registry import ToolRegistry


def _builtin_registry() -> ToolRegistry:
    return ToolRegistry(DEFAULT_TOOLS_DIR)


def _fake_config(tmp_path) -> dict:
    """A minimal but build_tools/build_tool_defs-complete config that resolves
    the bundled tools and needs no network."""
    return {
        "llm": {
            "roles": {
                "main": {
                    "provider": "anthropic",
                    "model": "claude-test-model",
                    "max_tokens": 1024,
                    "api_key_env": "ANTHROPIC_API_KEY",
                },
                "narrator": {
                    "provider": "anthropic",
                    "model": "claude-haiku-test",
                    "max_tokens": 256,
                    "api_key_env": "ANTHROPIC_API_KEY",
                },
            }
        },
        "memory": {"backend": "sqlite", "collection": "fabri"},
        "agent": {"max_steps": 7, "planner": {}},
        "tools": {
            "sandbox_root": str(tmp_path),
            "manifest_dir": ["builtin"],
            "enabled": None,
            "decompose": {"enabled": False},
        },
    }


# ---------------------------------------------------------------------------
# filter_tools / render_tools_listing
# ---------------------------------------------------------------------------


def test_filter_tools_lists_builtins():
    pairs = filter_tools(_builtin_registry())
    names = {name for name, _ in pairs}
    # A representative slice of the bundled tools (manifest `name`, not file stem).
    assert {"echo", "read_file", "write_file", "grep"} <= names
    # Pairs are sorted and every entry carries a description string.
    assert names  # non-empty
    assert pairs == sorted(pairs)
    assert all(isinstance(desc, str) for _, desc in pairs)


def test_filter_tools_search_is_substring_over_name_and_description():
    reg = _builtin_registry()
    hits = filter_tools(reg, "file")
    assert hits, "expected some tools matching 'file'"
    for name, desc in hits:
        assert "file" in name.lower() or "file" in desc.lower()
    # A subset of the full listing.
    assert set(hits) <= set(filter_tools(reg))


def test_filter_tools_search_no_match_returns_empty():
    assert filter_tools(_builtin_registry(), "zzz_no_such_tool_zzz") == []


def test_render_tools_listing_shows_count_and_names():
    pairs = filter_tools(_builtin_registry())
    out = render_tools_listing(pairs)
    assert f"{len(pairs)} tool(s) available:" in out
    assert "read_file" in out


def test_render_tools_listing_search_header_and_empty():
    assert "matching 'file'" in render_tools_listing(
        filter_tools(_builtin_registry(), "file"), search="file"
    )
    assert "no tools match" in render_tools_listing([], search="nope")
    assert "no tools available" in render_tools_listing([])


# ---------------------------------------------------------------------------
# cmd_tools (the CLI handler)
# ---------------------------------------------------------------------------


def test_cmd_tools_lists_builtins(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _p: _fake_config(tmp_path))
    args = argparse.Namespace(config=None, search=None, manifest_dir=["builtin"])
    cli.cmd_tools(args)
    out = capsys.readouterr().out
    assert "write_file" in out
    assert "read_file" in out
    assert "tool(s) available" in out


def test_cmd_tools_search_filters(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _p: _fake_config(tmp_path))
    args = argparse.Namespace(config=None, search="grep", manifest_dir=["builtin"])
    cli.cmd_tools(args)
    out = capsys.readouterr().out
    assert "grep" in out
    # Only the matching tool(s) are listed, not the full registry.
    assert "matching 'grep'" in out
    assert len(filter_tools(_builtin_registry(), "grep")) < len(filter_tools(_builtin_registry()))


def test_cmd_tools_parses_via_main(tmp_path, monkeypatch, capsys):
    """End-to-end through argparse: the `tools` subcommand + flags are wired."""
    monkeypatch.setattr(cli, "load_config", lambda _p: _fake_config(tmp_path))
    monkeypatch.setattr(sys, "argv", ["fabri", "tools", "--manifest-dir", "builtin"])
    cli.main()
    assert "read_file" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# build_dry_run_plan / render_dry_run_plan
# ---------------------------------------------------------------------------


def test_build_dry_run_plan_summarizes_config_and_tools():
    config = _fake_config(None)
    tool_defs = [
        {"name": "alpha", "description": "first", "input_schema": {"type": "object"}},
        {"name": "beta", "description": "second", "input_schema": {"type": "object"}},
    ]
    plan = build_dry_run_plan(config, tool_defs)
    assert set(plan["roles"]) == {"main", "narrator"}
    assert plan["roles"]["main"]["model"] == "claude-test-model"
    assert plan["memory"]["backend"] == "sqlite"
    assert plan["agent"]["max_steps"] == 7
    assert plan["tool_count"] == 2
    assert plan["tool_defs"] == tool_defs


def test_render_dry_run_plan_contains_markers_and_tool_names():
    config = _fake_config(None)
    tool_defs = [{"name": "alpha", "description": "first", "input_schema": {"type": "object"}}]
    out = render_dry_run_plan(build_dry_run_plan(config, tool_defs), task="do a thing")
    assert "dry run" in out.lower()
    assert "do a thing" in out
    assert "claude-test-model" in out
    assert "alpha" in out


# ---------------------------------------------------------------------------
# cmd_run --dry-run: no network, no API key
# ---------------------------------------------------------------------------


def _explode(*_a, **_k):
    raise AssertionError("dry-run must not reach the network / LLM / store")


def test_cmd_run_dry_run_no_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda _p: _fake_config(tmp_path))
    # Any of these being invoked on the dry-run path is a bug.
    monkeypatch.setattr(cli, "_require_role_api_keys", _explode)
    monkeypatch.setattr(cli, "_open_store", _explode)
    monkeypatch.setattr(cli, "build_run_llms", _explode)
    monkeypatch.setattr(cli, "run_agent", _explode)
    monkeypatch.setattr(cli, "process_trace", _explode)

    args = argparse.Namespace(
        config=None, task="inspect me", session_id=None,
        verbose=False, ask_user_socket=None, dry_run=True,
    )
    cli.cmd_run(args)  # must not raise SystemExit nor the guards above
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "inspect me" in out
    # The bundled tools show up as the defs that would be sent to the model.
    assert "read_file" in out


def test_cmd_run_dry_run_via_main(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda _p: _fake_config(tmp_path))
    monkeypatch.setattr(cli, "_require_role_api_keys", _explode)
    monkeypatch.setattr(cli, "_open_store", _explode)
    monkeypatch.setattr(cli, "run_agent", _explode)
    monkeypatch.setattr(sys, "argv", ["fabri", "run", "a task", "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "a task" in out


# ---------------------------------------------------------------------------
# tool run: positional cousin of tool test
# ---------------------------------------------------------------------------


def _scaffold_add_tool(tmp_path):
    """Scaffold a deterministic add(a, b) tool through the real tool-writer so
    `tool run` exercises the genuine runner/sandbox path."""
    from fabri.builder import new_tool

    sig = tmp_path / "fn.py"
    sig.write_text(
        "def add(a: int, b: int) -> int:\n"
        '    """Add two integers."""\n'
        "    return a + b\n"
    )
    out_dir = tmp_path / "tools"
    new_tool("add", from_signature=str(sig), target_dir=out_dir)
    return out_dir


def test_cmd_tool_run_invokes_tool(tmp_path, capsys):
    out_dir = _scaffold_add_tool(tmp_path)
    args = argparse.Namespace(name="add", json_args='{"a": 2, "b": 3}', dir=str(out_dir))
    cli.cmd_tool_run(args)
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert '"result": 5' in out


def test_cmd_tool_run_rejects_bad_json(tmp_path, capsys):
    out_dir = _scaffold_add_tool(tmp_path)
    args = argparse.Namespace(name="add", json_args="{not json", dir=str(out_dir))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_tool_run(args)
    assert exc.value.code != 0
    assert "<json-args>" in capsys.readouterr().err
