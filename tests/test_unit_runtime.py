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
