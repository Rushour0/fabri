"""Unit tests for G10 (fan-out telemetry) + G11 (delegation regret).

We don't spin up a real sub-agent (slow + requires API key); instead we drive
_dispatch_tool_calls directly with synthetic ToolCall lists, monkeypatch the
ToolRegistry.invoke to return controlled results, and assert that the
on_subagent_finished callback fires + emits a `delegation_regret` event when
the child wasted money on <2 steps.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from fabri.core.agent import SPAWN_SUBAGENT_TOOL_NAME, _dispatch_tool_calls
from fabri.core.llm import ToolCall


class _FakeRegistry:
    """Minimal ToolRegistry stand-in: every call returns a pre-canned result."""
    def __init__(self, result_for: dict[str, dict]):
        self.result_for = result_for

    def invoke(self, name, args):
        return self.result_for.get(name, {"ok": False, "error": f"no fake for {name}"})


def _call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, args=args, id=f"call_{name}_{uuid.uuid4().hex[:6]}")


def _trace_path(session_id: str) -> Path:
    """Mirrors orchestrator.traces.trace_path under the test's FABRI_HOME."""
    from fabri.paths import traces_dir
    return traces_dir() / f"{session_id}.jsonl"


def _read_events(session_id: str) -> list[dict]:
    path = _trace_path(session_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_on_subagent_finished_fires_for_successful_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    session_id = str(uuid.uuid4())
    captured = []

    def _on_finished(call, ok, child_usage):
        captured.append((call.name, ok, child_usage))

    registry = _FakeRegistry({
        SPAWN_SUBAGENT_TOOL_NAME: {"ok": True, "result": {
            "usage": {"total_cost_usd": 0.01, "step_count": 4},
        }},
    })
    _dispatch_tool_calls(
        [_call(SPAWN_SUBAGENT_TOOL_NAME, task="sub")],
        registry, llm=None, default_task="t", max_subquestions=1,
        session_id=session_id, messages=[], step_num=1,
        on_subagent_finished=_on_finished,
    )
    assert len(captured) == 1
    name, ok, usage = captured[0]
    assert name == SPAWN_SUBAGENT_TOOL_NAME
    assert ok is True
    assert usage["total_cost_usd"] == 0.01


def test_on_subagent_finished_fires_for_failed_spawn(tmp_path, monkeypatch):
    """G10: failed spawns count too — a host wants to see they were
    attempted, not just that they completed."""
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    session_id = str(uuid.uuid4())
    captured = []

    def _on_finished(call, ok, child_usage):
        captured.append((ok, child_usage))

    registry = _FakeRegistry({
        SPAWN_SUBAGENT_TOOL_NAME: {"ok": False, "error": "boom"},
    })
    _dispatch_tool_calls(
        [_call(SPAWN_SUBAGENT_TOOL_NAME, task="sub")],
        registry, llm=None, default_task="t", max_subquestions=1,
        session_id=session_id, messages=[], step_num=1,
        on_subagent_finished=_on_finished,
    )
    assert len(captured) == 1
    ok, usage = captured[0]
    assert ok is False
    assert usage is None  # failed spawn has no child usage


def test_delegation_regret_emitted_when_child_did_one_step(tmp_path, monkeypatch):
    """G11: a successful spawn that ran <=1 step but cost something fires a
    `delegation_regret` event. The pitch's whole "single-threaded by default"
    claim is empirically falsifiable via this event."""
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    session_id = str(uuid.uuid4())

    # We test the regret-firing logic by invoking the closure that
    # core.agent.run_agent builds. Easiest path: reach for the same callback
    # construction here.
    from fabri.orchestrator.traces import log_event
    regret_count = [0]

    def _on_finished(call, ok, child_usage):
        if not ok or not child_usage:
            return
        cost = child_usage.get("total_cost_usd") or child_usage.get("cost_usd") or 0.0
        if child_usage.get("step_count", 0) <= 1 and cost > 0:
            regret_count[0] += 1
            log_event(session_id, {
                "type": "delegation_regret",
                "tool": call.name,
                "child_step_count": child_usage.get("step_count", 0),
                "child_cost_usd": cost,
                "reason": "spawn ran <=1 step but cost >0; likely inlinable",
            })

    registry = _FakeRegistry({
        SPAWN_SUBAGENT_TOOL_NAME: {"ok": True, "result": {
            "usage": {"total_cost_usd": 0.005, "step_count": 1},
        }},
    })
    _dispatch_tool_calls(
        [_call(SPAWN_SUBAGENT_TOOL_NAME, task="sub")],
        registry, llm=None, default_task="t", max_subquestions=1,
        session_id=session_id, messages=[], step_num=1,
        on_subagent_finished=_on_finished,
    )
    assert regret_count[0] == 1
    events = _read_events(session_id)
    regrets = [e for e in events if e.get("type") == "delegation_regret"]
    assert len(regrets) == 1
    assert regrets[0]["child_step_count"] == 1
    assert regrets[0]["child_cost_usd"] == 0.005


def test_no_regret_when_child_did_real_work(tmp_path, monkeypatch):
    """The flip side: a sub-agent that ran multiple steps must NOT fire regret,
    so this telemetry doesn't cry wolf."""
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    session_id = str(uuid.uuid4())
    regret_count = [0]

    def _on_finished(call, ok, child_usage):
        if not ok or not child_usage:
            return
        cost = child_usage.get("total_cost_usd") or 0.0
        if child_usage.get("step_count", 0) <= 1 and cost > 0:
            regret_count[0] += 1

    registry = _FakeRegistry({
        SPAWN_SUBAGENT_TOOL_NAME: {"ok": True, "result": {
            "usage": {"total_cost_usd": 0.05, "step_count": 8},
        }},
    })
    _dispatch_tool_calls(
        [_call(SPAWN_SUBAGENT_TOOL_NAME, task="sub")],
        registry, llm=None, default_task="t", max_subquestions=1,
        session_id=session_id, messages=[], step_num=1,
        on_subagent_finished=_on_finished,
    )
    assert regret_count[0] == 0
