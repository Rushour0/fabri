"""tools.parallel_agents: same-turn tools.agents specialist calls fan out
through the parallel_group thread pool. Driven at the _dispatch_tool_calls
seam with a stub registry, so no Qdrant, subprocess, or API key is needed."""
import threading
import time

from fabri.core.agent import (
    AGENT_TOOLS_PARALLEL_GROUP,
    _dispatch_tool_calls,
    _index_parallel_groups,
)
from fabri.core.llm import ToolCall

SLEEP_S = 0.3


class _AgentToolRegistry:
    """Stand-in for agent-as-tool specialists: each call sleeps, records its
    start/end time, and returns the run envelope a real child would."""

    def __init__(self, costs: dict[str, float]):
        self._costs = costs
        self._lock = threading.Lock()
        self.spans: dict[str, tuple[float, float]] = {}

    def invoke(self, name: str, args: dict) -> dict:
        start = time.monotonic()
        time.sleep(SLEEP_S)
        end = time.monotonic()
        with self._lock:
            self.spans[name] = (start, end)
        return {"ok": True, "result": {
            "final_text": f"{name} done",
            "outcome": "success",
            "session_id": f"child-{name}",
            "usage": {"total_cost_usd": self._costs[name]},
        }}


def _calls():
    return [
        ToolCall(name="researcher", args={"task": "a"}, id="c1"),
        ToolCall(name="writer", args={"task": "b"}, id="c2"),
    ]


def _dispatch(reg, calls, *, parallel_agent_tools=(), on_budget_check=None):
    messages, costs = [], []
    t0 = time.monotonic()
    _dispatch_tool_calls(
        calls, reg, llm=None, default_task="t", max_subquestions=5,
        session_id="parallel-agents-test", messages=messages, step_num=0,
        on_subagent_cost=costs.append, on_budget_check=on_budget_check,
        parallel_agent_tools=parallel_agent_tools,
    )
    return time.monotonic() - t0, messages, costs


def _overlap(spans):
    (s1, e1), (s2, e2) = spans["researcher"], spans["writer"]
    return s1 < e2 and s2 < e1


def test_index_groups_agent_tools_only_when_named():
    calls = [*_calls(), ToolCall(name="read_file", args={}, id="c3")]
    assert _index_parallel_groups(calls) == {}
    assert _index_parallel_groups(calls, ("researcher", "writer")) == {
        AGENT_TOOLS_PARALLEL_GROUP: [0, 1],
    }


def test_agent_tools_run_concurrently_with_flag():
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2})
    elapsed, _, _ = _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assert _overlap(reg.spans)
    assert elapsed < 2 * SLEEP_S


def test_agent_tools_stay_serial_without_flag():
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2})
    elapsed, _, _ = _dispatch(reg, _calls())
    assert not _overlap(reg.spans)
    assert elapsed >= 2 * SLEEP_S


def test_parallel_results_keep_call_order_and_roll_up_cost_once():
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2})
    _, messages, costs = _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assistant, user = messages
    assert [b["id"] for b in assistant["content"]] == ["c1", "c2"]
    assert [b["tool_use_id"] for b in user["content"]] == ["c1", "c2"]
    assert "researcher done" in user["content"][0]["content"]
    assert "writer done" in user["content"][1]["content"]
    assert sorted(costs) == [0.1, 0.2]


def test_budget_tripwire_refuses_parallel_agent_tools():
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2})
    _, messages, costs = _dispatch(
        reg, _calls(), parallel_agent_tools=("researcher", "writer"),
        on_budget_check=lambda: True,
    )
    assert reg.spans == {}
    assert costs == []
    user = messages[1]["content"]
    assert all("cost budget exceeded" in b["content"] for b in user)
