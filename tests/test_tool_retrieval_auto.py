"""tools.retrieval.enabled: auto turns top-k tool retrieval on with embeddings
and enough tools, and keeps every tool visible without embeddings."""
from __future__ import annotations

import functools
import uuid
from pathlib import Path

import pytest

from fabri.config import DEFAULT_CONFIG
from fabri.core import agent as agent_mod
from fabri.core.llm import LLMResponse
from fabri.core.run_config import AgentRunConfig
from fabri.orchestrator import retrieval
from fabri.orchestrator.retrieval import tool_retrieval_active, tool_retrieval_mode

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


def test_mode_parsing():
    assert tool_retrieval_mode(True) == "on" and tool_retrieval_mode("on") == "on"
    assert tool_retrieval_mode(False) == "off" and tool_retrieval_mode("off") == "off"
    assert tool_retrieval_mode(None) == "auto" and tool_retrieval_mode("AUTO") == "auto"
    with pytest.raises(ValueError):
        tool_retrieval_mode("sometimes")


def test_auto_needs_embeddings_and_more_tools_than_the_threshold():
    assert tool_retrieval_active("auto", 20, auto_threshold=15, embeddings=True) is True
    assert tool_retrieval_active("auto", 15, auto_threshold=15, embeddings=True) is False
    assert tool_retrieval_active("auto", 200, auto_threshold=15, embeddings=False) is False
    # The explicit switches ignore embeddings and the count.
    assert tool_retrieval_active(False, 200, embeddings=True) is False
    assert tool_retrieval_active(True, 1, embeddings=False) is True


def test_auto_uses_the_installed_embeddings_check(monkeypatch):
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: False)
    assert tool_retrieval_active("auto", 100) is False
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    assert tool_retrieval_active("auto", 100) is True


def test_plain_config_stays_off_and_library_default_is_off():
    assert DEFAULT_CONFIG["tools"]["retrieval"]["enabled"] is False
    assert AgentRunConfig.from_config({}).tool_retrieval_enabled is False
    assert AgentRunConfig.from_config({"tools": {"retrieval": {"top_k": 4}}}).tool_retrieval_enabled is False
    rc = AgentRunConfig.from_config({"tools": {"retrieval": {"enabled": "auto", "auto_threshold": 3}}})
    assert rc.tool_retrieval_enabled == "auto" and rc.tool_retrieval_auto_threshold == 3
    assert rc.as_kwargs()["tool_retrieval_auto_threshold"] == 3
    with pytest.raises(ValueError):
        AgentRunConfig.from_config({"tools": {"retrieval": {"enabled": "maybe"}}})


@pytest.mark.parametrize("value,expected", [(None, 15), (0, 0), (7, 7), ("4", 4)])
def test_threshold_null_means_default_and_zero_is_kept(value, expected):
    rc = AgentRunConfig.from_config({"tools": {"retrieval": {"enabled": "auto", "auto_threshold": value}}})
    assert rc.tool_retrieval_auto_threshold == expected
    assert retrieval.tool_retrieval_threshold(value) == expected


def _registry(tmp_path, monkeypatch, retrieval_cfg):
    from fabri.config import _deep_merge
    from fabri.runtime import build_tools

    monkeypatch.setenv("FABRI_SANDBOX_ROOT", str(tmp_path))
    import copy
    cfg = _deep_merge(copy.deepcopy(DEFAULT_CONFIG["tools"]),
                      {"manifest_dir": str(EXAMPLES_DIR), "sandbox_root": str(tmp_path), "retrieval": retrieval_cfg})
    return build_tools(cfg)


def test_plain_run_gets_find_tools_only_when_retrieval_can_be_on(tmp_path, monkeypatch):
    off = _registry(tmp_path, monkeypatch, {})
    assert "find_tools" not in off.tools and "describe_tool" not in off.tools
    for enabled in (True, "auto"):
        on = _registry(tmp_path, monkeypatch, {"enabled": enabled})
        assert {"find_tools", "describe_tool"} <= set(on.tools)


def test_plain_find_tools_ranks_and_exposes(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, {"enabled": True})
    found = reg.tools["find_tools"]
    result = reg.invoke("find_tools", {"query": "read a file", "k": 3})
    assert result["ok"], result
    names = [t["name"] for t in result["result"]["tools"]]
    assert "read_file" in names and "find_tools" not in names
    assert "read_file" in (reg.pop_exposed() or [])
    described = reg.invoke("describe_tool", {"name": "read_file"})
    assert described["ok"] and described["result"]["input_schema"]
    assert found.approval == "read"


def test_setup_agents_default_to_auto_and_explicit_false_wins(tmp_path):
    from fabri.setups import build_agent_config, resolve_setup

    base = tmp_path / "base"
    (base / "agents" / "helper").mkdir(parents=True)
    (base / "agents" / "helper" / "agent.yaml").write_text("max_steps: 3\n")
    (base / "tools").mkdir()
    (base / "tools" / "tools.yaml").write_text("sources: []\n")
    layers = [("b", base)]
    cfg, registry = build_agent_config(resolve_setup(layers), "helper", {"provider": "openai", "model": "x"})
    mode = cfg["tools"]["retrieval"]["enabled"]
    # auto only when the agent has find_tools to reach hidden tools.
    assert mode == ("auto" if "find_tools" in registry.tools else False)
    cfg, _ = build_agent_config(resolve_setup(layers), "helper", {"provider": "openai", "model": "x"},
                                base_config={"tools": {"retrieval": {"enabled": False}}})
    assert cfg["tools"]["retrieval"]["enabled"] is False


def test_retrieval_always_keeps_the_meta_tools(tmp_path, monkeypatch):
    from fabri import QdrantMemoryStore, run_agent

    reg = _registry(tmp_path, monkeypatch, {"enabled": True})
    seen = {}

    def fake_retrieve(task, registry, *, top_k, always_include):
        seen["always"] = set(always_include)
        return list(registry.list())[:top_k]

    monkeypatch.setattr(agent_mod, "retrieve_tools", fake_retrieve)
    run_agent("x", _Capture(), reg, QdrantMemoryStore(collection=f"auto_{uuid.uuid4().hex[:10]}"),
              tool_retrieval_enabled=True, tool_retrieval_top_k=1, tool_retrieval_always_include=("mine",))
    assert seen["always"] >= {"mine", "ask_user", "spawn_subagent", "decompose", "find_tools", "describe_tool"}


class _Capture:
    def __init__(self):
        self.system = None

    def step(self, system, messages):
        self.system = self.system or system
        return LLMResponse(final_text="done")


def _listed(system: str) -> list[str]:
    block = system.split("Available tools:")[1].split("\n\n")[0]
    return [line[2:].split(":")[0] for line in block.splitlines() if line.startswith("- ")]


@pytest.mark.parametrize("embeddings", [True, False])
def test_run_agent_auto_narrows_only_with_embeddings(tmp_path, monkeypatch, embeddings):
    from fabri import QdrantMemoryStore, run_agent
    from fabri.tools.registry import ToolRegistry

    monkeypatch.setenv("FABRI_SANDBOX_ROOT", str(tmp_path))
    tools = ToolRegistry(EXAMPLES_DIR)
    total = len(tools.list())
    assert total > 3
    monkeypatch.setattr(agent_mod, "tool_retrieval_active",
                        functools.partial(tool_retrieval_active, embeddings=embeddings))
    # The real ranker needs the embeddings model; take the first k instead.
    monkeypatch.setattr(agent_mod, "retrieve_tools",
                        lambda task, registry, *, top_k, always_include: list(registry.list())[:top_k])
    backend = _Capture()
    run_agent("read a file", backend, tools, QdrantMemoryStore(collection=f"auto_{uuid.uuid4().hex[:10]}"),
              tool_retrieval_enabled="auto", tool_retrieval_top_k=2, tool_retrieval_auto_threshold=3,
              tool_retrieval_always_include=())
    assert len(_listed(backend.system)) == (2 if embeddings else total)
