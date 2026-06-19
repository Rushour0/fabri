"""Unit tests for the run_agent outcome classifier. The three values
(success / success_with_recovery / incomplete) are observable behavior the
pipeline keys off of, so the rules deserve direct tests rather than only
incidental coverage via the e2e suite."""
import uuid
from pathlib import Path

import pytest

from agent_memory import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from agent_memory.core.llm import LLMResponse, ToolCall
from agent_memory.tools.manifest_schema import ToolManifest


def _store():
    return QdrantMemoryStore(collection=f"outcome_{uuid.uuid4().hex[:8]}")


def _registry_with_always_ok():
    reg = ToolRegistry([])
    # 'true' exits 0 but prints no JSON -> registry normalizes to ok=False,
    # malformed-output failure. For a real ok=True we need a tiny inline tool.
    reg.register(ToolManifest(
        name="noop", description="ok",
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print(json.dumps({}))"],
        input_schema={}, output_schema={},
    ))
    reg.register(ToolManifest(
        name="fails", description="fails",
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print(json.dumps({})); sys.exit(1)"],
        input_schema={}, output_schema={},
    ))
    return reg


def test_outcome_success_when_no_tool_calls():
    reg = ToolRegistry([])
    r = run_agent("x", ScriptedLLMBackend([LLMResponse(final_text="done")]), reg, _store())
    assert r["outcome"] == "success"


def test_outcome_success_when_all_tools_succeed():
    reg = _registry_with_always_ok()
    script = [
        LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1")),
        LLMResponse(final_text="done"),
    ]
    r = run_agent("x", ScriptedLLMBackend(script), reg, _store())
    assert r["outcome"] == "success"


def test_outcome_success_with_recovery_when_a_tool_failed_but_final_text_produced():
    reg = _registry_with_always_ok()
    script = [
        LLMResponse(tool_call=ToolCall(name="fails", args={}, id="t1")),
        LLMResponse(final_text="recovered"),
    ]
    r = run_agent("x", ScriptedLLMBackend(script), reg, _store())
    assert r["outcome"] == "success_with_recovery"


def test_outcome_incomplete_when_max_steps_exhausted_without_final_text():
    reg = _registry_with_always_ok()
    script = [LLMResponse(tool_call=ToolCall(name="noop", args={}, id=f"t{i}")) for i in range(10)]
    r = run_agent("x", ScriptedLLMBackend(script), reg, _store(), max_steps=2)
    assert r["outcome"] == "incomplete"
    assert r["success"] is False
    assert r["final_text"] is None


def test_session_id_returned_and_can_be_supplied():
    reg = ToolRegistry([])
    r = run_agent("x", ScriptedLLMBackend([LLMResponse(final_text="d")]), reg, _store(), session_id="fixed-sid")
    assert r["session_id"] == "fixed-sid"
