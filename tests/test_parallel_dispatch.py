"""F2 -- parallel_group dispatch. Three angles:

1. Index helper groups spawn_subagent calls by parallel_group; non-spawn
   calls and ungrouped spawn calls are excluded.
2. The runner loop dispatches grouped calls concurrently. We measure wall
   clock: three 0.5s sleeps run in well under 1.5s.
3. tool_call events for parallel calls carry the parallel_group tag, so a
   trace-tail viewer can group them visually.
"""
import time
import uuid

import pytest

from fabri import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.agent import _index_parallel_groups
from fabri.core.llm import LLMResponse, ToolCall
from fabri.orchestrator.traces import read_trace
from fabri.tools.manifest_schema import ToolManifest


def _store():
    return QdrantMemoryStore(collection=f"f2_{uuid.uuid4().hex[:8]}")


def _slow_spawn_tool(seconds: float) -> ToolManifest:
    """A stand-in for spawn_subagent: sleeps `seconds`, prints empty JSON,
    exits ok. We name it `spawn_subagent` so the F2 detection in
    _index_parallel_groups picks it up (the dispatcher only fans out tool
    calls named spawn_subagent)."""
    return ToolManifest(
        name="spawn_subagent",
        description="fake slow spawn for parallel_group tests",
        command=[
            "python3", "-c",
            f"import sys,json,time; sys.stdin.read(); time.sleep({seconds}); print(json.dumps({{'ok':True}}))",
        ],
        input_schema={"type": "object", "properties": {"parallel_group": {"type": "string"}}},
        output_schema={"type": "object"},
        timeout_s=10,
    )


def _registry_with(manifest: ToolManifest) -> ToolRegistry:
    reg = ToolRegistry([])
    reg.register(manifest)
    return reg


def test_index_parallel_groups_picks_only_grouped_spawns():
    calls = [
        ToolCall(name="read_file", args={"path": "x"}, id="a"),
        ToolCall(name="spawn_subagent", args={"task": "x"}, id="b"),  # no group
        ToolCall(name="spawn_subagent", args={"task": "y", "parallel_group": "g1"}, id="c"),
        ToolCall(name="spawn_subagent", args={"task": "z", "parallel_group": "g1"}, id="d"),
        ToolCall(name="spawn_subagent", args={"task": "w", "parallel_group": "g2"}, id="e"),
    ]
    groups = _index_parallel_groups(calls)
    assert groups == {"g1": [2, 3], "g2": [4]}


def _time_dispatch_via_registry(reg, n_calls: int, parallel_group: str | None) -> float:
    """Time only the dispatch loop, not run_agent's setup (embedding model
    load + Qdrant warm-up dominate wall clock on first invocation). We call
    _dispatch_tool_calls directly with a stub LLM, so the measurement is
    the raw tool-fanout cost."""
    from fabri.core.agent import _dispatch_tool_calls

    calls = [
        ToolCall(
            name="spawn_subagent",
            args=({"parallel_group": parallel_group} if parallel_group else {}),
            id=f"t{i}",
        )
        for i in range(n_calls)
    ]
    t0 = time.monotonic()
    _dispatch_tool_calls(
        calls,
        reg,
        llm=ScriptedLLMBackend([]),
        default_task="t",
        max_subquestions=3,
        session_id=f"f2-test-{uuid.uuid4().hex[:8]}",
        messages=[],
        step_num=0,
    )
    return time.monotonic() - t0


def test_grouped_spawns_run_concurrently():
    """Three 0.5s spawns in the same group should finish in well under their
    serial total (1.5s). Timed via the dispatch loop directly so the
    embedding model load doesn't get blamed on the fan-out."""
    reg = _registry_with(_slow_spawn_tool(0.5))
    elapsed = _time_dispatch_via_registry(reg, n_calls=3, parallel_group="g1")
    assert elapsed < 1.2, f"expected concurrent <1.2s, got {elapsed:.2f}s"


def test_ungrouped_spawns_run_serially():
    """Sanity check the inverse: same three spawns without parallel_group
    should NOT overlap (no behavior regression for callers who don't opt in)."""
    reg = _registry_with(_slow_spawn_tool(0.3))
    elapsed = _time_dispatch_via_registry(reg, n_calls=3, parallel_group=None)
    assert elapsed >= 0.8, f"expected serial >=0.8s, got {elapsed:.2f}s"


def test_parallel_group_tag_in_trace_events():
    reg = _registry_with(_slow_spawn_tool(0.05))
    script = [
        LLMResponse(tool_calls=[
            ToolCall(name="spawn_subagent", args={"parallel_group": "village"}, id="t1"),
            ToolCall(name="spawn_subagent", args={"parallel_group": "village"}, id="t2"),
        ]),
        LLMResponse(final_text="done"),
    ]
    r = run_agent("x", ScriptedLLMBackend(script), reg, _store())
    trace = read_trace(r["session_id"])
    tool_call_events = [e for e in trace if e.get("type") == "tool_call"]
    assert len(tool_call_events) == 2
    assert all(e.get("parallel_group") == "village" for e in tool_call_events)
