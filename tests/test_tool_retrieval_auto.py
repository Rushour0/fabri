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


def test_config_default_is_auto_and_false_still_turns_it_off():
    assert DEFAULT_CONFIG["tools"]["retrieval"]["enabled"] == "auto"
    rc = AgentRunConfig.from_config({"tools": {"retrieval": {}}})
    assert rc.tool_retrieval_enabled == "auto" and rc.tool_retrieval_auto_threshold == 15
    rc = AgentRunConfig.from_config({"tools": {"retrieval": {"enabled": False, "auto_threshold": 3}}})
    assert rc.tool_retrieval_enabled is False and rc.tool_retrieval_auto_threshold == 3
    assert rc.as_kwargs()["tool_retrieval_auto_threshold"] == 3


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
