"""End-to-end tests for run_agent driven by a ScriptedLLMBackend against real
sandboxed file tools. These exercise the full loop -- tool dispatch, the
tool_use/tool_result message protocol, sandbox enforcement, decompose, and
outcome classification -- without needing an LLM API key. A live Qdrant
instance is required (the existing test suite already assumes this)."""
import os
import uuid
from pathlib import Path

import pytest

from fabri import (
    AgentProtocolError,
    QdrantMemoryStore,
    ScriptedLLMBackend,
    ToolRegistry,
    run_agent,
)
from fabri.core.llm import LLMError, LLMResponse, ToolCall

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


def _sandbox(tmp_path: Path) -> ToolRegistry:
    # path-jail tools read FABRI_SANDBOX_ROOT at subprocess-spawn time, so the
    # env var must be set before tools.invoke() forks each call.
    os.environ["FABRI_SANDBOX_ROOT"] = str(tmp_path)
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


class _CaptureBackend:
    """Records the messages it's handed on each step; runs one tool then finishes."""
    def __init__(self):
        self.n = 0
        self.system = None
        self.messages_at_finish = None

    def step(self, system, messages):
        self.n += 1
        if self.n == 1:
            self.system = system
            return LLMResponse(tool_calls=[ToolCall(name="list_dir", args={"path": "."}, id="t1")])
        self.messages_at_finish = messages
        return LLMResponse(final_text="done")


def _injected_tool_result(messages):
    return messages[-1]["content"][0]["content"]


def test_tool_results_injected_as_toon_by_default(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    tools = _sandbox(tmp_path)
    backend = _CaptureBackend()
    run_agent("inspect", backend, tools, _store())  # result_format defaults to toon
    injected = _injected_tool_result(backend.messages_at_finish)
    # the list_dir entries array renders as a TOON table, not a JSON object
    assert not injected.lstrip().startswith("{")
    assert "entries[1]{name,is_dir}:" in injected
    assert "TOON" in backend.system  # the model is told the result format


def test_result_format_json_opts_out(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    tools = _sandbox(tmp_path)
    backend = _CaptureBackend()
    run_agent("inspect", backend, tools, _store(), result_format="json")
    injected = _injected_tool_result(backend.messages_at_finish)
    assert injected.lstrip().startswith("{")  # plain JSON
    assert backend.system is not None and "TOON" not in backend.system


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


def test_run_agent_planner_mode_force_emits_plan_events(tmp_path):
    # A2: planner_mode='force' runs one plan() call up-front then one executor
    # step-loop per plan item. Trace must carry plan_started, two
    # plan_item_started/finished pairs, and plan_finished -- in that order.
    tools = _sandbox(tmp_path)
    store = _store()
    planner_plan = (
        '{"items": ['
        '{"goal": "write a.txt", "artifacts": ["a.txt"], "depends_on": []},'
        '{"goal": "write b.txt", "artifacts": ["b.txt"], "depends_on": [0]}'
        ']}'
    )
    # Main script: planner emits the JSON plan first, then the executor runs
    # one write_file + one final_text per plan item.
    script = [
        LLMResponse(final_text=planner_plan),  # planner step
        LLMResponse(tool_call=ToolCall(name="write_file", args={"path": "a.txt", "content": "A"}, id="t1")),
        LLMResponse(final_text="wrote a"),
        LLMResponse(tool_call=ToolCall(name="write_file", args={"path": "b.txt", "content": "B"}, id="t2")),
        LLMResponse(final_text="wrote b"),
    ]
    backend = ScriptedLLMBackend(script)
    result = run_agent(
        "build a then build b", backend, tools, store,
        planner_mode="force", max_steps=10,
    )
    assert result["success"] is True
    assert (tmp_path / "a.txt").read_text() == "A"
    assert (tmp_path / "b.txt").read_text() == "B"

    from fabri.events import EventType
    from fabri.orchestrator.traces import read_trace
    events = read_trace(result["session_id"])
    types = [e["type"] for e in events]
    assert EventType.PLAN_STARTED.value in types
    assert types.count(EventType.PLAN_ITEM_STARTED.value) == 2
    assert types.count(EventType.PLAN_ITEM_FINISHED.value) == 2
    assert EventType.PLAN_FINISHED.value in types
    # plan_started must precede the first plan_item_started; plan_finished must
    # land before the usage event at run end.
    assert types.index(EventType.PLAN_STARTED.value) < types.index(EventType.PLAN_ITEM_STARTED.value)
    assert types.index(EventType.PLAN_FINISHED.value) < types.index(EventType.USAGE.value)


def test_run_agent_planner_mode_off_leaves_legacy_path_unchanged(tmp_path):
    # planner_mode='off' (default) must produce zero plan events.
    tools = _sandbox(tmp_path)
    backend = ScriptedLLMBackend([LLMResponse(final_text="done")])
    result = run_agent("x", backend, tools, _store())
    from fabri.events import EventType
    from fabri.orchestrator.traces import read_trace
    types = {e["type"] for e in read_trace(result["session_id"])}
    assert EventType.PLAN_STARTED.value not in types


def test_run_agent_with_tool_retrieval_narrows_system_prompt(tmp_path):
    # A1: enabling tool_retrieval restricts the descriptions in the system
    # prompt to the task-relevant subset. The full set has 7+ tools; with
    # top_k=2 the prompt must reference at most that many (+ always_include).
    tools = _sandbox(tmp_path)
    backend = _CaptureBackend()
    run_agent(
        "read a file from disk",
        backend,
        tools,
        _store(),
        tool_retrieval_enabled=True,
        tool_retrieval_top_k=2,
        tool_retrieval_always_include=(),
    )
    sys_prompt = backend.system
    # Only the top-2 task-relevant tools end up in "Available tools:".
    available_block = sys_prompt.split("Available tools:")[1].split("\n\n")[0]
    listed = [line[2:].split(":")[0] for line in available_block.splitlines() if line.startswith("- ")]
    assert len(listed) == 2
    assert "read_file" in listed  # the obviously relevant tool survives


def test_run_agent_emits_usage_event_and_returns_usage(tmp_path):
    # A5: every run ends with a `usage` event and the return dict carries a
    # `usage` subobject with the per-run token + cost fields.
    tools = _sandbox(tmp_path)
    store = _store()
    script = [LLMResponse(final_text="hello")]
    result = run_agent("usage", ScriptedLLMBackend(script), tools, store)

    usage = result["usage"]
    assert set(usage.keys()) == {
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        "step_count", "wall_time_s",
        # COGS: own-token cost, per-model breakdown, rolled-up sub-agent cost,
        # and the end-to-end total a host persists as the build's cost.
        "cost_usd", "cost_by_model", "subagent_cost_usd", "total_cost_usd",
        # G4: guideline reuse rate (cross-session learning signal).
        "guideline_reuse_rate", "guidelines_retrieved", "guidelines_from_prior_sessions",
    }
    assert usage["step_count"] == 1
    assert usage["wall_time_s"] >= 0
    # Scripted backend leaves LLMResponse.usage=None, so token totals stay 0 and
    # every cost field is 0 with no per-model entries.
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0.0
    assert usage["subagent_cost_usd"] == 0.0
    assert usage["total_cost_usd"] == 0.0
    assert usage["cost_by_model"] == {}

    from fabri.events import EventType
    from fabri.orchestrator.traces import read_trace
    events = read_trace(result["session_id"])
    assert events[-1]["type"] == EventType.USAGE.value
    assert events[-1]["step_count"] == 1


def test_run_agent_usage_aggregates_across_steps(tmp_path):
    # When the backend reports per-call usage, run_agent sums it across every step.
    from fabri.core.llm import LLMUsage

    tools = _sandbox(tmp_path)
    store = _store()
    script = [
        LLMResponse(
            tool_call=ToolCall(name="list_dir", args={"path": "."}, id="t1"),
            usage=LLMUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=3),
        ),
        LLMResponse(
            final_text="done",
            usage=LLMUsage(input_tokens=20, output_tokens=4, cache_creation_input_tokens=7),
        ),
    ]
    result = run_agent("usage-agg", ScriptedLLMBackend(script), tools, store)
    usage = result["usage"]
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 9
    assert usage["cache_read_input_tokens"] == 3
    assert usage["cache_creation_input_tokens"] == 7
    assert usage["step_count"] == 2


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
