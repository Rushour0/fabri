"""tools.parallel_agents: same-turn tools.agents specialist calls fan out
through the parallel_group thread pool. Driven at the _dispatch_tool_calls
seam with a stub registry, so no Qdrant, subprocess, or API key is needed."""
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fabri.core.agent import (
    AGENT_TOOLS_PARALLEL_GROUP,
    _dispatch_tool_calls,
    _index_parallel_groups,
)
from fabri.core.llm import ToolCall
from fabri.tools.agent_tool import make_agent_tool_manifest

SLEEP_S = 0.3


class _AgentToolRegistry:
    """Stand-in for agent-as-tool specialists: each call sleeps, records its
    start/end time, and returns the run envelope a real child would."""

    def __init__(self, costs: dict[str, float], configs: dict[str, Path] | None = None):
        self._costs = costs
        # name -> specialist manifest, so the one-writer rule can classify
        # each specialist by its child config.
        self.tools = {
            name: make_agent_tool_manifest({"name": name, "description": "d", "config": str(cfg)})
            for name, cfg in (configs or {}).items()
        }
        self.args: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.spans: dict[str, tuple[float, float]] = {}

    def invoke(self, name: str, args: dict) -> dict:
        start = time.monotonic()
        with self._lock:
            self.args[name] = dict(args)
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


READER_CFG = """
    tools:
      manifest_dir: [builtin]
      enabled: [read_file, grep]
      sandbox_root: .
"""
WRITER_CFG = """
    tools:
      manifest_dir: [builtin]
      enabled: [read_file, write_file]
      sandbox_root: .
"""


def _cfg(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


@pytest.fixture
def readers(tmp_path):
    cfg = _cfg(tmp_path, "reader.yaml", READER_CFG)
    return {"researcher": cfg, "writer": cfg}


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


def test_agent_tools_run_concurrently_with_flag(readers):
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2}, readers)
    elapsed, _, _ = _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assert _overlap(reg.spans)
    assert elapsed < 2 * SLEEP_S


def test_agent_tools_stay_serial_without_flag():
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2})
    elapsed, _, _ = _dispatch(reg, _calls())
    assert not _overlap(reg.spans)
    assert elapsed >= 2 * SLEEP_S


def test_parallel_results_keep_call_order_and_roll_up_cost_once(readers):
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2}, readers)
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


def test_reader_specialists_get_read_only_scope_pinned(readers):
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2}, readers)
    _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assert reg.args["researcher"]["tool_scope"] == ["read_only"]
    assert reg.args["writer"]["tool_scope"] == ["read_only"]


def test_two_writer_specialists_never_overlap(tmp_path):
    writer = _cfg(tmp_path, "writer.yaml", WRITER_CFG)
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2},
                             {"researcher": writer, "writer": writer})
    elapsed, _, costs = _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assert not _overlap(reg.spans)
    assert elapsed >= 2 * SLEEP_S
    assert sorted(costs) == [0.1, 0.2]
    assert "tool_scope" not in reg.args["researcher"]
    assert "tool_scope" not in reg.args["writer"]


def test_one_writer_runs_alongside_readers(tmp_path):
    reader = _cfg(tmp_path, "reader.yaml", READER_CFG)
    writer = _cfg(tmp_path, "writer.yaml", WRITER_CFG)
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2},
                             {"researcher": reader, "writer": writer})
    _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assert _overlap(reg.spans)
    assert reg.args["researcher"]["tool_scope"] == ["read_only"]
    assert "tool_scope" not in reg.args["writer"]


def test_unknown_specialist_manifest_is_a_writer():
    # No manifests: nothing can be shown to read, so both are writers.
    reg = _AgentToolRegistry({"researcher": 0.1, "writer": 0.2})
    _dispatch(reg, _calls(), parallel_agent_tools=("researcher", "writer"))
    assert not _overlap(reg.spans)
