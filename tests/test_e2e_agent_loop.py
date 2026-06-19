"""End-to-end tests for run_agent driven by a ScriptedLLMBackend against real
sandboxed file tools. These exercise the full loop -- tool dispatch, the
tool_use/tool_result message protocol, sandbox enforcement, decompose, and
outcome classification -- without needing an LLM API key. A live Qdrant
instance is required (the existing test suite already assumes this)."""
import os
import uuid
from pathlib import Path

import pytest

from agent_memory import (
    AgentProtocolError,
    QdrantMemoryStore,
    ScriptedLLMBackend,
    ToolRegistry,
    run_agent,
)
from agent_memory.core.llm import LLMError, LLMResponse, ToolCall

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_memory" / "tools" / "examples"


def _sandbox(tmp_path: Path) -> ToolRegistry:
    # path-jail tools read AGENT_SANDBOX_ROOT at subprocess-spawn time, so the
    # env var must be set before tools.invoke() forks each call.
    os.environ["AGENT_SANDBOX_ROOT"] = str(tmp_path)
    return ToolRegistry(EXAMPLES_DIR)


def _store() -> QdrantMemoryStore:
    # ephemeral per-test collection so runs don't pollute each other or any
    # real memory collection on the shared Qdrant.
    return QdrantMemoryStore(collection=f"e2e_{uuid.uuid4().hex[:10]}")


def test_run_agent_write_then_read_through_sandbox(tmp_path):
    tools = _sandbox(tmp_path)
    store = _store()
    script = [
        LLMResponse(tool_call=ToolCall(name="write_file", args={"path": "note.txt", "content": "hi"}, id="t1")),
        LLMResponse(tool_call=ToolCall(name="read_file", args={"path": "note.txt"}, id="t2")),
        LLMResponse(final_text="wrote and read note.txt"),
    ]
    result = run_agent("write+read", ScriptedLLMBackend(script), tools, store)
    assert result["outcome"] == "success"
    assert (tmp_path / "note.txt").read_text() == "hi"


def test_run_agent_sandbox_escape_is_recoverable_failure(tmp_path):
    tools = _sandbox(tmp_path)
    store = _store()
    script = [
        LLMResponse(tool_call=ToolCall(name="read_file", args={"path": "../escape.txt"}, id="t1")),
        LLMResponse(final_text="gave up after escape attempt"),
    ]
    result = run_agent("escape", ScriptedLLMBackend(script), tools, store)
    # tool fails (exit 1) but the agent still produces final_text -- classification
    # promotes from SUCCESS to SUCCESS_WITH_RECOVERY exactly because of this.
    assert result["outcome"] == "success_with_recovery"


def test_run_agent_edit_file_unique_match_enforced(tmp_path):
    (tmp_path / "f.txt").write_text("alpha beta alpha")
    tools = _sandbox(tmp_path)
    store = _store()
    # First call: ambiguous old string -> tool error. Second: replace_all=true succeeds.
    script = [
        LLMResponse(tool_call=ToolCall(name="edit_file", args={"path": "f.txt", "old": "alpha", "new": "X"}, id="t1")),
        LLMResponse(tool_call=ToolCall(name="edit_file", args={"path": "f.txt", "old": "alpha", "new": "X", "replace_all": True}, id="t2")),
        LLMResponse(final_text="done"),
    ]
    result = run_agent("edit", ScriptedLLMBackend(script), tools, store)
    assert result["outcome"] == "success_with_recovery"
    assert (tmp_path / "f.txt").read_text() == "X beta X"


def test_run_agent_incomplete_when_step_budget_exhausted(tmp_path):
    tools = _sandbox(tmp_path)
    store = _store()
    # Every step calls a tool, never produces final_text -- after max_steps=3
    # the loop exits with INCOMPLETE rather than crashing.
    script = [
        LLMResponse(tool_call=ToolCall(name="list_dir", args={"path": "."}, id=f"t{i}"))
        for i in range(10)
    ]
    result = run_agent("loop forever", ScriptedLLMBackend(script), tools, store, max_steps=3)
    assert result["outcome"] == "incomplete"
    assert result["final_text"] is None


def test_run_agent_malformed_llm_response_raises_protocol_error(tmp_path):
    tools = _sandbox(tmp_path)
    store = _store()
    script = [LLMResponse()]  # neither tool_call nor final_text
    with pytest.raises(AgentProtocolError):
        run_agent("nonsense", ScriptedLLMBackend(script), tools, store)


def test_run_agent_empty_final_text_is_not_a_false_success(tmp_path):
    # An empty string is not a real answer; it must not be reported as SUCCESS.
    tools = _sandbox(tmp_path)
    store = _store()
    with pytest.raises(AgentProtocolError):
        run_agent("empty", ScriptedLLMBackend([LLMResponse(final_text="")]), tools, store)


def test_run_agent_parallel_tool_calls_in_one_turn(tmp_path):
    # A single LLM turn emitting two tool_use blocks must run BOTH tools, then
    # append exactly one assistant turn + one user turn pairing every result.
    tools = _sandbox(tmp_path)
    store = _store()
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall(name="write_file", args={"path": "a.txt", "content": "A"}, id="t1"),
                ToolCall(name="write_file", args={"path": "b.txt", "content": "B"}, id="t2"),
            ]
        ),
        LLMResponse(final_text="wrote both"),
    ]
    result = run_agent("parallel", ScriptedLLMBackend(script), tools, store)
    assert result["outcome"] == "success"
    assert (tmp_path / "a.txt").read_text() == "A"
    assert (tmp_path / "b.txt").read_text() == "B"


def test_run_agent_llm_error_maps_to_failed_outcome(tmp_path):
    # A provider error mid-run ends the run as FAILED, not a raw traceback.
    tools = _sandbox(tmp_path)
    store = _store()

    class _BoomBackend:
        def step(self, system, messages):
            raise LLMError("simulated rate limit")

    result = run_agent("boom", _BoomBackend(), tools, store)
    assert result["outcome"] == "failed"
    assert result["success"] is False


def test_multi_dir_registry_merges_manifests(tmp_path):
    # Build a second manifest dir on disk with one tool and confirm both dirs'
    # tools end up in one registry -- the wiring the framework relies on for
    # "framework tools + project tools" composition.
    extra = tmp_path / "extra_tools"
    extra.mkdir()
    (extra / "noop.json").write_text('{"name":"noop","description":"x","command":["python3","-c","import sys,json;sys.stdin.read();print(json.dumps({}))"],"input_schema":{"type":"object"},"output_schema":{"type":"object"},"timeout_s":5}')
    reg = ToolRegistry([EXAMPLES_DIR, extra])
    names = set(reg.tools)
    assert {"read_file", "write_file", "noop"}.issubset(names)
