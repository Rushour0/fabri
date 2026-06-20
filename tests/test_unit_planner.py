"""Unit tests for core/planner.py (A2). The planner emits a structured JSON
plan; these pin the parse contract + topological ordering."""
import pytest

from fabri.core.llm import LLMResponse, ScriptedLLMBackend
from fabri.core.planner import PlanItem, plan, topological_order


def test_plan_parses_clean_json():
    body = (
        '{"items": ['
        '{"goal": "build map", "artifacts": ["map.json"], "depends_on": [], "tool_hints": ["write_file"]},'
        '{"goal": "add npc", "artifacts": ["npc.toml"], "depends_on": [0], "tool_hints": []}'
        ']}'
    )
    llm = ScriptedLLMBackend([LLMResponse(final_text=body)])
    items = plan("forest map + npc", llm)
    assert len(items) == 2
    assert items[0].goal == "build map"
    assert items[1].depends_on == [0]
    assert items[0].tool_hints == ["write_file"]


def test_plan_handles_markdown_fenced_json():
    body = '```json\n{"items": [{"goal": "g"}]}\n```'
    llm = ScriptedLLMBackend([LLMResponse(final_text=body)])
    items = plan("x", llm)
    assert items == [PlanItem(goal="g")]


def test_plan_caps_at_max_items():
    body = '{"items": [' + ",".join('{"goal": "g%d"}' % i for i in range(20)) + "]}"
    llm = ScriptedLLMBackend([LLMResponse(final_text=body)])
    items = plan("x", llm, max_items=3)
    assert len(items) == 3
    assert [i.goal for i in items] == ["g0", "g1", "g2"]


def test_plan_falls_back_to_single_item_on_malformed():
    llm = ScriptedLLMBackend([LLMResponse(final_text="not json at all")])
    items = plan("buff hero STR by 5", llm)
    # Fallback preserves forward progress: one item whose goal is the task.
    assert len(items) == 1
    assert items[0].goal == "buff hero STR by 5"


def test_plan_ignores_items_missing_goal():
    body = '{"items": [{"goal": "ok"}, {"artifacts": ["x"]}, {"goal": ""}, {"goal": "ok2"}]}'
    llm = ScriptedLLMBackend([LLMResponse(final_text=body)])
    items = plan("x", llm)
    assert [i.goal for i in items] == ["ok", "ok2"]


def test_topological_order_respects_deps():
    items = [
        PlanItem(goal="A", depends_on=[1]),  # A depends on B
        PlanItem(goal="B"),
        PlanItem(goal="C", depends_on=[0, 1]),  # C depends on A,B
    ]
    order = topological_order(items)
    # B (1) must come before A (0); A must come before C (2).
    assert order.index(1) < order.index(0) < order.index(2)


def test_topological_order_falls_back_on_cycle():
    # A -> B -> A cycle. Topological order can't be valid; we accept declared
    # order rather than raising so the executor still gets to try.
    items = [
        PlanItem(goal="A", depends_on=[1]),
        PlanItem(goal="B", depends_on=[0]),
    ]
    order = topological_order(items)
    assert sorted(order) == [0, 1]
