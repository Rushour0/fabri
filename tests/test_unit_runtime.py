"""Unit tests for runtime.build_tool_defs and build_llm -- the bits cli.py
and agent_runner_tool.py share. Mostly about ensuring the decompose
synthetic tool def is added/omitted correctly and that the llm provider
switch raises on unknowns rather than silently returning None."""

import pytest

from fabri.core.agent import DECOMPOSE_TOOL_NAME
from fabri.runtime import build_decompose_llm, build_llm, build_tool_defs, build_tools
from fabri.tools.registry import ToolRegistry
from fabri.tools.manifest_schema import ToolManifest


def _registry_with(names):
    reg = ToolRegistry([])
    for n in names:
        reg.register(ToolManifest(name=n, description=f"desc:{n}", command=["true"], input_schema={}, output_schema={}))
    return reg


def test_build_tool_defs_basic_shape():
    reg = _registry_with(["a", "b"])
    defs = build_tool_defs(reg, {"enabled": False})
    assert [d["name"] for d in defs] == ["a", "b"]
    assert defs[0]["description"] == "desc:a"
    assert defs[0]["input_schema"] == {"type": "object"}


def test_build_tool_defs_appends_decompose_when_enabled():
    reg = _registry_with(["a"])
    defs = build_tool_defs(reg, {"enabled": True})
    assert defs[-1]["name"] == DECOMPOSE_TOOL_NAME
    assert "task" in defs[-1]["input_schema"]["properties"]


def test_build_tool_defs_omits_decompose_when_disabled():
    reg = _registry_with(["a"])
    defs = build_tool_defs(reg, {"enabled": False})
    assert all(d["name"] != DECOMPOSE_TOOL_NAME for d in defs)


def test_build_tool_defs_empty_registry():
    defs = build_tool_defs(_registry_with([]), {"enabled": False})
    assert defs == []


def test_build_llm_unknown_provider_raises():
    cfg = {"llm": {"provider": "bogus", "model": "m", "max_tokens": 1, "api_key_env": "K"}}
    with pytest.raises(ValueError, match="unknown llm provider"):
        build_llm(cfg, [])


def test_build_tools_handles_string_manifest_dir(tmp_path):
    # config from YAML may give one path as a plain string, not a list.
    (tmp_path / "t.json").write_text('{"name":"t","description":"d","command":["true"]}')
    reg = build_tools({
        "manifest_dir": str(tmp_path),
        "enabled": None,
        "sandbox_root": str(tmp_path),
        "agents": [],
        "decompose": {"enabled": False, "max_subquestions": 5},
    })
    assert "t" in reg.tools


def test_build_tools_resolves_sandbox_to_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = build_tools({
        "manifest_dir": [],
        "enabled": None,
        "sandbox_root": "sub/dir",
        "agents": [],
        "decompose": {"enabled": False, "max_subquestions": 5},
    })
    # Stored on the registry as an absolute path (later threaded into each tool
    # subprocess's env=); deliberately NOT set on os.environ.
    assert reg.sandbox_root == str((tmp_path / "sub" / "dir").resolve())


def test_build_decompose_llm_returns_none_when_unset():
    cfg = {"llm": {"provider": "anthropic", "model": "m", "max_tokens": 1, "api_key_env": "K"}}
    assert build_decompose_llm(cfg) is None


def test_build_decompose_llm_uses_override_model(monkeypatch):
    # Stub out anthropic so we don't need the SDK / key; verify the override
    # model is what reaches the backend.
    captured = {}

    class _Stub:
        def __init__(self, model, tools, max_tokens, api_key_env, **kwargs):
            # **kwargs absorbs G21-era additions (cache_messages) so this
            # stub doesn't need touching every time we widen the constructor.
            captured["model"] = model
            captured["max_tokens"] = max_tokens

    monkeypatch.setattr("fabri.runtime.AnthropicLLMBackend", _Stub)
    cfg = {"llm": {"provider": "anthropic", "model": "claude-sonnet-4-6",
                   "decompose_model": "claude-haiku-4-5",
                   "max_tokens": 1024, "api_key_env": "ANTHROPIC_API_KEY"}}
    backend = build_decompose_llm(cfg)
    assert backend is not None
    assert captured["model"] == "claude-haiku-4-5"


def test_build_tools_agents_default_to_empty(tmp_path):
    # tools.agents is optional; build_tools must tolerate its absence (legacy
    # configs from before the agent-as-tool feature existed).
    reg = build_tools({
        "manifest_dir": [],
        "enabled": None,
        "sandbox_root": str(tmp_path),
        "decompose": {"enabled": False, "max_subquestions": 5},
    })
    assert reg.tools == {}


# --- D0 wiring: approver, MCP approval classes, tools.sources ---------------

import logging
import sys
from pathlib import Path

FAKE_MCP = Path(__file__).parent / "fixtures" / "mcp" / "fake_mcp_server.py"


def _tools_cfg(tmp_path, **extra):
    return {
        "manifest_dir": [],
        "enabled": None,
        "sandbox_root": str(tmp_path),
        "decompose": {"enabled": False, "max_subquestions": 5},
        **extra,
    }


def test_build_tools_passes_approver(tmp_path):
    def approver(name, args, manifest):
        return False

    assert build_tools(_tools_cfg(tmp_path), approver=approver).approver is approver
    assert build_tools(_tools_cfg(tmp_path)).approver is None


def test_build_tools_mcp_tools_get_approval_class(tmp_path):
    reg = build_tools(_tools_cfg(tmp_path, mcp_servers=[
        {"name": "tracker", "command": [sys.executable, str(FAKE_MCP)]},
    ]))
    try:
        approvals = {name: m.approval for name, m in reg.tools.items()}
        assert approvals == {
            "mcp_tracker_search_issues": "write",
            "mcp_tracker_create_issue": "write",
            "mcp_tracker_archive_issue": "always_approve",
            "mcp_tracker_send_message": "always_approve",
        }
        # With no approver the gate refuses the send before it reaches the server.
        result = reg.invoke("mcp_tracker_send_message", {"text": "hi"})
        assert result["ok"] is False
        assert "no approver is configured" in result["error"]
    finally:
        # The registry has no close; stop the server child it owns.
        for client in reg._owned_resources:
            client.close()


def _slack_post(approval):
    return {"kind": "http", "name": "notify", "url": "https://slack.com/api/chat.postMessage",
            "method": "POST", "approval": approval,
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}}


def test_build_tools_registers_config_sources_with_class_and_floor(tmp_path):
    lookup = {"kind": "http", "name": "weather", "url": "https://api.example.test/weather/{city}",
              "method": "GET", "approval": "read",
              "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}
    reg = build_tools(_tools_cfg(tmp_path, sources=[lookup, _slack_post("read")]))
    # The declared class stays when no floor applies.
    assert reg.tools["weather"].approval == "read"
    # A POST to a catalog-owned host is always_approve whatever the YAML says.
    assert reg.tools["notify"].approval == "always_approve"

    seen = []

    def decline(name, args, manifest):
        seen.append(name)
        return False

    reg.approver = decline
    result = reg.invoke("notify", {"text": "hi"})
    assert result["ok"] is False
    assert "a human declined this call" in result["error"]
    assert seen == ["notify"]


def test_build_tools_sources_use_strict_policy_by_default(tmp_path, caplog):
    py = {"kind": "python", "name": "py_tool", "callable": "os.getcwd"}
    with caplog.at_level(logging.WARNING, logger="fabri.runtime"):
        reg = build_tools(_tools_cfg(tmp_path, sources=[py, _slack_post("always_approve")]))
    assert "py_tool" not in reg.tools
    assert "notify" in reg.tools
    assert any("py_tool" in r.getMessage() for r in caplog.records)


def test_build_tools_sources_apply_config_policy(tmp_path):
    get = {"kind": "http", "name": "lookup", "url": "https://api.example.test/x", "method": "GET",
           "approval": "read"}
    other = {"kind": "http", "name": "other", "url": "https://api.example.test/y", "method": "GET"}
    policy = {"allow": ["lookup"], "approval_floor": {"lookup": "always_approve"}}
    reg = build_tools(_tools_cfg(tmp_path, sources=[get, other], policy=policy))
    assert set(reg.tools) == {"lookup"}
    assert reg.tools["lookup"].approval == "always_approve"

    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("allowed_kinds: [openapi, http, python]\npython_modules: [os]\n")
    py = {"kind": "python", "name": "py_tool", "callable": "os.getcwd"}
    reg = build_tools(_tools_cfg(tmp_path, sources=[py], policy=str(policy_file)))
    assert reg.tools["py_tool"].approval == "write"


def test_build_tools_sources_never_replace_a_registered_tool(tmp_path):
    agent = {"name": "notify", "description": "a sub-agent", "config": "agent.yaml"}
    reg = build_tools(_tools_cfg(tmp_path, agents=[agent], sources=[_slack_post("always_approve")]))
    assert reg.tools["notify"].description == "a sub-agent"
    assert "notify" not in reg._callables


def test_build_tools_rejects_bad_sources_shape(tmp_path):
    with pytest.raises(ValueError, match="tools.sources must be a list"):
        build_tools(_tools_cfg(tmp_path, sources={"kind": "http"}))
