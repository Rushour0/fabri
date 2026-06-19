"""End-to-end tests for the config loader and the admin gate -- the two
seams a consuming project depends on when wiring a new agent up."""
import os
from pathlib import Path

import pytest

from agent_memory import (
    AdminAuthError,
    DEFAULT_CONFIG,
    describe_config,
    load_config,
    make_agent_tool_manifest,
    render_dashboard,
    require_admin,
)
from agent_memory.admin import ADMIN_TOKEN_ENV
from agent_memory.runtime import build_tools

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_memory" / "tools" / "examples"


def test_load_config_deep_merges_over_defaults(tmp_path):
    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text("agent:\n  name: custom\ntools:\n  enabled: [read_file]\n")
    merged = load_config(str(cfg_path))
    assert merged["agent"]["name"] == "custom"
    # defaults preserved for fields the user didn't override
    assert merged["agent"]["max_steps"] == DEFAULT_CONFIG["agent"]["max_steps"]
    assert merged["llm"]["provider"] == DEFAULT_CONFIG["llm"]["provider"]
    assert merged["tools"]["enabled"] == ["read_file"]


def test_load_config_none_returns_defaults():
    assert load_config(None) is DEFAULT_CONFIG


def test_build_tools_filters_to_enabled_set(tmp_path):
    cfg = {
        "manifest_dir": str(EXAMPLES_DIR),
        "enabled": ["read_file", "write_file"],
        "sandbox_root": str(tmp_path),
        "agents": [],
        "decompose": {"enabled": False, "max_subquestions": 5},
    }
    reg = build_tools(cfg)
    assert set(reg.tools) == {"read_file", "write_file"}
    # AGENT_SANDBOX_ROOT was set as a side effect for the file tools to read.
    assert os.environ["AGENT_SANDBOX_ROOT"] == str(tmp_path.resolve())


def test_build_tools_registers_agent_as_tool(tmp_path):
    sub_cfg = tmp_path / "sub.yaml"
    sub_cfg.write_text("agent:\n  name: sub\n")
    cfg = {
        "manifest_dir": str(EXAMPLES_DIR),
        "enabled": None,
        "sandbox_root": str(tmp_path),
        "agents": [{"name": "sub_agent", "description": "delegate", "config": str(sub_cfg)}],
        "decompose": {"enabled": False, "max_subquestions": 5},
    }
    reg = build_tools(cfg)
    assert "sub_agent" in reg.tools
    cmd = reg.tools["sub_agent"].command
    # sub-agent runs through agent_runner_tool.py, not as a plain script
    assert any("agent_runner_tool.py" in part for part in cmd)
    assert str(sub_cfg.resolve()) in cmd


def test_make_agent_tool_manifest_shape():
    m = make_agent_tool_manifest({"name": "a", "description": "d", "config": "x.yaml", "timeout_s": 99})
    assert m.name == "a"
    assert m.timeout_s == 99
    assert m.input_schema["required"] == ["task"]


def test_require_admin_open_by_default(monkeypatch):
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    require_admin(None)  # no exception


def test_require_admin_enforces_when_token_env_set(monkeypatch):
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "secret")
    with pytest.raises(AdminAuthError):
        require_admin(None)
    with pytest.raises(AdminAuthError):
        require_admin("wrong")
    require_admin("secret")  # correct token passes


def test_describe_config_marks_agent_tools(tmp_path):
    sub_cfg = tmp_path / "sub.yaml"
    sub_cfg.write_text("agent:\n  name: sub\n")
    cfg = {
        "agent": {"name": "parent", "max_steps": 5, "system_prompt_prefix": ""},
        "llm": {"provider": "anthropic", "model": "m", "max_tokens": 1, "api_key_env": "K"},
        "tools": {
            "manifest_dir": str(EXAMPLES_DIR),
            "enabled": ["read_file", "sub_agent"],
            "sandbox_root": str(tmp_path),
            "agents": [{"name": "sub_agent", "description": "d", "config": str(sub_cfg)}],
            "decompose": {"enabled": False, "max_subquestions": 5},
        },
    }
    reg = build_tools(cfg["tools"])
    desc = describe_config(cfg, reg)
    by_name = {t["name"]: t for t in desc["tools"]}
    assert by_name["sub_agent"]["is_agent_tool"] is True
    assert by_name["read_file"]["is_agent_tool"] is False


def test_render_dashboard_includes_tools_and_memory(tmp_path):
    from agent_memory import QdrantMemoryStore
    import uuid

    sub_cfg = tmp_path / "sub.yaml"
    sub_cfg.write_text("agent:\n  name: sub\n")
    cfg = {
        "agent": {"name": "parent", "max_steps": 5, "system_prompt_prefix": ""},
        "llm": {"provider": "anthropic", "model": "m", "max_tokens": 1, "api_key_env": "K"},
        "tools": {
            "manifest_dir": str(EXAMPLES_DIR),
            "enabled": ["read_file", "sub_agent"],
            "sandbox_root": str(tmp_path),
            "agents": [{"name": "sub_agent", "description": "delegate", "config": str(sub_cfg)}],
            "decompose": {"enabled": True, "max_subquestions": 3},
        },
        "memory": {"collection": f"dash_{uuid.uuid4().hex[:8]}", "qdrant_url": "http://localhost:6333"},
    }
    reg = build_tools(cfg["tools"])
    store = QdrantMemoryStore(collection=cfg["memory"]["collection"])
    rendered = render_dashboard(cfg, reg, store)
    assert "parent" in rendered
    assert "[tool] read_file" in rendered
    assert "[agent] sub_agent" in rendered
    assert "decompose: on" in rendered
