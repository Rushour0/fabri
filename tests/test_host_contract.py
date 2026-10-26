"""The surface fabri-playbook (pba) uses today. These must keep passing as the
core changes underneath, so a host that patches fabri the old way still works.

Offline: scripted backends, a provider backend built with a fake key and never
called, and retrieval switched off so no store is touched."""
from __future__ import annotations

import yaml

from fabri.config import load_config
from fabri.core.agent import run_agent
from fabri.core.llm import LLMResponse, ScriptedLLMBackend, ToolCall
from fabri.core.run_config import AgentRunConfig
from fabri.runtime import build_run_llms, build_tool_defs, build_tools
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_ok


def _loaded(tmp_path, provider: str = "openai") -> dict:
    config = {
        "llm": {"provider": provider, "model": "gpt-5-mini", "api_key_env": "FABRI_HOST_CONTRACT_KEY", "max_tokens": 256},
        "agent": {"max_steps": 4, "system_prompt": "You help.", "planner": {"enabled": False}},
        "tools": {"manifest_dir": [], "sandbox_root": str(tmp_path), "enabled": None},
        "memory": {"retrieval_enabled": False},
    }
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(config))
    loaded = load_config(str(path))
    loaded["llm"]["roles"]["narrator"] = None
    return loaded


def _registry(loaded: dict, calls: list):
    registry = build_tools(loaded["tools"])
    manifest = ToolManifest(
        "Update canvas", "Update a canvas", [], {"type": "object"}, {"type": "object"},
        aliases=["admin_canvases_update"], title="Update canvas",
    )
    registry.register_callable(manifest, lambda args: tool_ok(calls.append(args) or {"status": "ok"}))
    return registry


def test_run_agent_with_config_kwargs_and_no_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    loaded = _loaded(tmp_path)
    calls: list = []
    registry = _registry(loaded, calls)
    backend = ScriptedLLMBackend([
        LLMResponse(tool_call=ToolCall("admin_canvases_update", {"id": 7})),
        LLMResponse(final_text="Canvas updated."),
    ])
    result = run_agent(
        "update it", tools=registry, store=None, llm=backend,
        decompose_llm=None, planner_llm=None, narrator_llm=None,
        **AgentRunConfig.from_config(loaded).as_kwargs(),
    )
    assert result["final_text"] == "Canvas updated."
    assert result["outcome"] == "success"
    assert calls == [{"id": 7}]


def test_instance_invoke_override_is_used_by_the_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    loaded = _loaded(tmp_path)
    registry = _registry(loaded, [])
    invoke = registry.invoke
    seen: list[str] = []

    def invoke_with_unavailable(name: str, args: dict) -> dict:
        seen.append(name)
        if name.startswith("admin_") and name != "admin_canvases_update":
            return tool_ok({"status": "not_available", "op": name})
        return invoke(name, args)

    registry.invoke = invoke_with_unavailable
    backend = ScriptedLLMBackend([
        LLMResponse(tool_call=ToolCall("admin_canvases_delete", {"id": 1})),
        LLMResponse(final_text="Not available."),
    ])
    result = run_agent(
        "delete it", tools=registry, store=None, llm=backend,
        **AgentRunConfig.from_config(loaded).as_kwargs(),
    )
    assert seen == ["admin_canvases_delete"]
    assert result["outcome"] == "success"


def test_instance_step_swap_is_used_by_the_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    loaded = _loaded(tmp_path)
    backend = ScriptedLLMBackend([LLMResponse(final_text="never reached")])
    original = backend.step
    steps: list[int] = []

    def gated(system: str, messages: list) -> LLMResponse:
        steps.append(len(messages))
        return LLMResponse(final_text="Done early.")

    backend.step = gated
    try:
        result = run_agent(
            "go", tools=_registry(loaded, []), store=None, llm=backend,
            **AgentRunConfig.from_config(loaded).as_kwargs(),
        )
    finally:
        backend.step = original
    assert steps == [1]
    assert result["final_text"] == "Done early."
    assert backend.step == original


def test_provider_backend_exposes_tools_list(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOST_CONTRACT_KEY", "sk-test-not-used")
    loaded = _loaded(tmp_path)
    registry = _registry(loaded, [])
    llms = build_run_llms(loaded, build_tool_defs(registry, loaded["tools"].get("decompose", {})))
    assert set(llms) >= {"llm", "decompose_llm", "planner_llm", "narrator_llm"}
    tools = getattr(llms["llm"], "_tools", None)
    assert isinstance(tools, list) and tools
    assert any("Update" in str(t) for t in tools)
