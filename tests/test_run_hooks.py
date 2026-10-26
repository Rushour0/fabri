"""RunHooks: each hook fires with the right args, Stop and Reject work, batch
inner calls reach the tool hooks, context reaches thread-pool dispatch, and a
registry used outside a run behaves as before. Offline, scripted backends."""
from __future__ import annotations

import threading

import pytest

from fabri import Reject, RunHooks, ScriptedLLMBackend, Stop, run_agent
from fabri.core.llm import LLMResponse, LLMUsage, ToolCall
from fabri.orchestrator.traces import read_trace
from fabri.tools.result import tool_ok
from fabri.tools.run_context import current_call, current_run
from tests.golden_scenarios import ColdStore, golden_path, make_registry, run_normalized


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))


def _run(llm, registry, hooks, **kwargs):
    return run_agent("task", llm, registry, ColdStore(), hooks=hooks, **kwargs)


def test_step_hooks_and_on_llm_call_fire_with_args(tmp_path):
    seen: list = []
    first = LLMResponse(tool_call=ToolCall("echo", {"x": 1}), usage=LLMUsage(input_tokens=5))
    final = LLMResponse(final_text="done")
    hooks = RunHooks(
        before_step=lambda step, messages: seen.append(("before_step", step, len(messages))),
        after_step=lambda step, response: seen.append(("after_step", step, response)),
        on_llm_call=lambda kind, response, ms: seen.append(("llm", kind, response, ms >= 0)),
    )
    result = _run(ScriptedLLMBackend([first, final]), make_registry(tmp_path), hooks)
    assert result["final_text"] == "done"
    assert seen == [
        ("before_step", 0, 1),
        ("llm", "step", first, True),
        ("after_step", 0, first),
        ("before_step", 1, 3),
        ("llm", "step", final, True),
        ("after_step", 1, final),
    ]


def test_stop_from_before_step_ends_run_without_a_model_call(tmp_path):
    class NoCalls(ScriptedLLMBackend):
        def step(self, system, messages):
            raise AssertionError("model must not be called")

    llm_calls: list = []
    hooks = RunHooks(
        before_step=lambda step, messages: Stop("staged, all done"),
        on_llm_call=lambda *a: llm_calls.append(a),
    )
    result = run_agent(
        "task", NoCalls([]), make_registry(tmp_path), ColdStore(), hooks=hooks,
        session_id="hook-stop", response_schema={"type": "object"},
        repair={"enabled": True, "max_attempts": 2},
    )
    assert result["final_text"] == "staged, all done"
    assert result["outcome"] == "success"
    assert result["structured_output"] is None
    assert result["usage"]["step_count"] == 0
    assert llm_calls == []
    events = read_trace("hook-stop")
    types = [e["type"] for e in events]
    assert "step_started" not in types
    assert "structured_output" not in types
    assert "repair_attempt" not in types
    final = next(e for e in events if e["type"] == "final")
    assert final["stop"] == "hook"


def test_stop_after_tools_step_ends_run(tmp_path):
    llm = ScriptedLLMBackend([LLMResponse(tool_call=ToolCall("echo", {"x": 1}))])
    hooks = RunHooks(after_step=lambda step, response: Stop("enough") if response.tool_calls else None)
    result = _run(llm, make_registry(tmp_path), hooks)
    assert result["final_text"] == "enough"
    assert result["outcome"] == "success"
    assert result["usage"]["step_count"] == 1


def test_planner_stop_breaks_item_loop(tmp_path):
    plan = ScriptedLLMBackend([LLMResponse(final_text='{"items": [{"goal": "one"}, {"goal": "two"}]}')])
    llm = ScriptedLLMBackend([LLMResponse(final_text="one done")])
    steps: list[int] = []

    def before_step(step, messages):
        steps.append(step)
        return Stop("stopped") if step == 1 else None

    result = run_agent(
        "task", llm, make_registry(tmp_path), ColdStore(), hooks=RunHooks(before_step=before_step),
        planner_mode="force", planner_llm=plan, session_id="plan-stop",
    )
    assert steps == [0, 1]
    assert result["final_text"] == "one done\n\nstopped"
    assert result["outcome"] == "success"
    finished = [e for e in read_trace("plan-stop") if e["type"] == "plan_item_finished"]
    assert finished[-1]["stop"] == "hook"


def test_on_unknown_tool_result_is_returned(tmp_path):
    seen: list = []

    def on_unknown(name, args):
        seen.append((name, args))
        return tool_ok({"status": "not_available", "op": name}) if name.startswith("admin_") else None

    llm = ScriptedLLMBackend([
        LLMResponse(tool_calls=[ToolCall("admin_gone", {"id": 1}), ToolCall("other", {})]),
        LLMResponse(final_text="ok"),
    ])
    _run(llm, make_registry(tmp_path), RunHooks(on_unknown_tool=on_unknown), session_id="unknown")
    results = {e["name"]: e["result"] for e in read_trace("unknown") if e["type"] == "tool_call"}
    assert results["admin_gone"] == {"ok": True, "result": {"status": "not_available", "op": "admin_gone"}}
    assert results["other"] == {"ok": False, "error": "unknown tool: other"}
    assert seen == [("admin_gone", {"id": 1}), ("other", {})]


def test_tool_hooks_see_batch_inner_calls_not_the_wrapper(tmp_path):
    before: list = []
    after: list = []
    hooks = RunHooks(
        before_tool=lambda call: before.append(call),
        after_tool=lambda call, result: after.append((call.canonical_name, call.batch_index, result["ok"])),
    )
    llm = ScriptedLLMBackend([
        LLMResponse(tool_calls=[
            ToolCall("echo_alias", {"x": 0}, id="t0"),
            ToolCall("batch", {"calls": [
                {"name": "echo", "args": {"y": 1}},
                {"name": "echo_alias", "args": {"y": 2}},
            ]}, id="t1"),
        ]),
        LLMResponse(final_text="ok"),
    ])
    _run(llm, make_registry(tmp_path), hooks)
    assert [(c.invoked_name, c.canonical_name, c.call_index, c.batch_index, c.tool_call_id, c.step, c.args)
            for c in before] == [
        ("echo_alias", "echo", 0, None, "t0", 0, {"x": 0}),
        ("echo", "echo", 1, 0, "t1", 0, {"y": 1}),
        ("echo_alias", "echo", 1, 1, "t1", 0, {"y": 2}),
    ]
    assert after == [("echo", None, True), ("echo", 0, True), ("echo", 1, True)]
    assert all(c.run is before[0].run and c.run.session_id for c in before)


def test_before_tool_reject_and_after_tool_replace(tmp_path):
    ran: list = []
    registry = make_registry(tmp_path)
    registry.register_callable(registry.tools["echo"], lambda args: ran.append(args) or tool_ok(args))
    hooks = RunHooks(
        before_tool=lambda call: Reject("not today") if call.args.get("block") else None,
        after_tool=lambda call, result: {**result, "seen": True},
    )
    llm = ScriptedLLMBackend([
        LLMResponse(tool_calls=[ToolCall("echo", {"block": True}), ToolCall("echo", {"x": 1})]),
        LLMResponse(final_text="ok"),
    ])
    _run(llm, registry, hooks, session_id="reject")
    results = [e["result"] for e in read_trace("reject") if e["type"] == "tool_call"]
    assert results == [
        {"ok": False, "error": "echo: rejected: not today", "seen": True},
        {"ok": True, "result": {"x": 1}, "seen": True},
    ]
    assert ran == [{"x": 1}]


def test_raising_tool_hook_becomes_a_tool_error(tmp_path):
    def boom(call):
        raise RuntimeError("bad hook")

    llm = ScriptedLLMBackend([LLMResponse(tool_call=ToolCall("echo", {})), LLMResponse(final_text="ok")])
    result = _run(llm, make_registry(tmp_path), RunHooks(before_tool=boom), session_id="boom")
    (event,) = [e for e in read_trace("boom") if e["type"] == "tool_call"]
    assert event["result"] == {"ok": False, "error": "echo: before_tool hook raised RuntimeError: bad hook"}
    assert result["outcome"] == "success_with_recovery"


def test_context_reaches_thread_pool_parallel_dispatch(tmp_path):
    registry = make_registry(tmp_path)
    seen: dict = {}
    lock = threading.Lock()

    def spawn(args):
        run, call = current_run(), current_call()
        with lock:
            seen[args["task"]] = (run, call.call_index, call.canonical_name, threading.get_ident())
        return tool_ok({"spawned": args["task"]})

    registry.register_callable(registry.tools["spawn_subagent"], spawn)
    tool_seen: list = []
    llm = ScriptedLLMBackend([
        LLMResponse(tool_calls=[
            ToolCall("spawn_subagent", {"task": "a", "parallel_group": "g"}),
            ToolCall("spawn_subagent", {"task": "b", "parallel_group": "g"}),
        ]),
        LLMResponse(final_text="ok"),
    ])
    _run(llm, registry, RunHooks(before_tool=lambda call: tool_seen.append(call.call_index)), session_id="par")
    assert set(seen) == {"a", "b"}
    assert seen["a"][0] is not None and seen["a"][0] is seen["b"][0]
    assert seen["a"][0].session_id == "par"
    assert (seen["a"][1], seen["b"][1]) == (0, 1)
    assert seen["a"][2] == "spawn_subagent"
    assert sorted(tool_seen) == [0, 1]
    assert current_run() is None


def test_no_run_context_means_invoke_unchanged(tmp_path):
    registry = make_registry(tmp_path)
    assert current_run() is None
    seen: list = []
    registry.register_callable(registry.tools["echo"], lambda args: seen.append(current_call()) or tool_ok(args))
    assert registry.invoke("echo_alias", {"a": 1}) == {"ok": True, "result": {"a": 1}}
    assert registry.invoke("nope", {}) == {"ok": False, "error": "unknown tool: nope"}
    assert registry.invoke("batch", {"calls": [{"name": "echo", "args": {"b": 2}}]}) == {
        "ok": True, "result": {"results": [{"ok": True, "result": {"b": 2}}]},
    }
    assert seen == [None, None]


def test_run_context_is_reset_after_the_run(tmp_path):
    llm = ScriptedLLMBackend([LLMResponse(final_text="ok")])
    _run(llm, make_registry(tmp_path), RunHooks())
    assert current_run() is None and current_call() is None


@pytest.mark.parametrize("name", ["plain", "planner", "repair"])
def test_empty_hooks_keep_golden_traces(name, tmp_path):
    assert run_normalized(name, tmp_path, hooks=RunHooks()) == golden_path(name).read_text()


def test_observing_hooks_keep_golden_traces(tmp_path):
    calls: list = []
    hooks = RunHooks(
        before_step=lambda *a: calls.append("before_step"),
        after_step=lambda *a: calls.append("after_step"),
        on_llm_call=lambda *a: calls.append("llm"),
        before_tool=lambda *a: calls.append("before_tool"),
        after_tool=lambda *a: calls.append("after_tool"),
    )
    assert run_normalized("plain", tmp_path, hooks=hooks) == golden_path("plain").read_text()
    # echo, echo_alias, the known batch entry, two spawns. Unknown names
    # (nope, nope2) and the batch wrapper skip the tool hooks.
    assert calls.count("before_tool") == 5 and calls.count("after_tool") == 5
    assert calls.count("llm") == 3
