"""Scripted runs whose normalized traces are pinned under
tests/fixtures/golden_traces/. The goldens were generated from the code before
run hooks existed, so a run with no hooks must still produce them byte for byte.

Regenerate (only when a trace change is intended):
    FABRI_REGEN_GOLDENS=1 uv run --frozen pytest tests/test_golden_traces.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from fabri import ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.llm import LLMResponse, LLMUsage, ToolCall
from fabri.orchestrator.traces import read_trace
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_ok

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_traces"
VOLATILE_KEYS = ("ts", "event_id")
TIMING_KEYS = ("elapsed_s", "wall_time_s")


class ColdStore:
    """count()==0 keeps retrieval a no-op: no embeddings, no Qdrant."""

    def count(self) -> int:
        return 0


def _manifest(name: str, aliases: list[str] | None = None) -> ToolManifest:
    return ToolManifest(
        name, f"{name} tool", [], {"type": "object"}, {"type": "object"},
        aliases=aliases or [],
    )


def make_registry(tmp_path: Path) -> ToolRegistry:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    registry = ToolRegistry(manifest_dir)
    registry.register_callable(_manifest("echo", ["echo_alias"]), lambda args: tool_ok({"echo": args}))
    registry.register_callable(_manifest("spawn_subagent"), lambda args: tool_ok({"spawned": args.get("task")}))
    # The batch manifest only has to exist; invoke routes "batch" to invoke_batch.
    registry.register(_manifest("batch"))
    return registry


def _usage(n: int) -> LLMUsage:
    return LLMUsage(input_tokens=100 * n, output_tokens=10 * n, model="claude-sonnet-4-6")


def plain_scenario(tmp_path: Path, **run_kwargs) -> dict:
    llm = ScriptedLLMBackend([
        LLMResponse(
            tool_calls=[
                ToolCall("echo", {"x": 1}),
                ToolCall("echo_alias", {"x": 2}),
                ToolCall("nope", {}),
                ToolCall("batch", {"calls": [
                    {"name": "echo", "args": {"y": 1}},
                    {"name": "nope2", "args": {}},
                ]}),
            ],
            thinking_text="Calling a few tools.",
            usage=_usage(1),
        ),
        LLMResponse(
            tool_calls=[
                ToolCall("spawn_subagent", {"task": "a", "parallel_group": "g"}),
                ToolCall("spawn_subagent", {"task": "b", "parallel_group": "g"}),
            ],
            usage=_usage(2),
        ),
        LLMResponse(final_text="all done", usage=_usage(3)),
    ])
    return run_agent(
        "plain task", llm, make_registry(tmp_path), ColdStore(),
        session_id="golden-plain", **run_kwargs,
    )


def planner_scenario(tmp_path: Path, **run_kwargs) -> dict:
    plan_body = json.dumps({"items": [
        {"goal": "first goal", "artifacts": ["a.txt"]},
        {"goal": "second goal", "depends_on": [0]},
    ]})
    planner = ScriptedLLMBackend([LLMResponse(final_text=plan_body, usage=_usage(1))])
    llm = ScriptedLLMBackend([
        LLMResponse(tool_call=ToolCall("echo", {"item": 0}), usage=_usage(1)),
        LLMResponse(final_text="first done", usage=_usage(1)),
        LLMResponse(final_text="second done", usage=_usage(1)),
    ])
    return run_agent(
        "planner task", llm, make_registry(tmp_path), ColdStore(),
        session_id="golden-planner", planner_mode="force", planner_llm=planner,
        **run_kwargs,
    )


def repair_scenario(tmp_path: Path, **run_kwargs) -> dict:
    llm = ScriptedLLMBackend([
        LLMResponse(tool_call=ToolCall("echo", {"try": 1}), usage=_usage(1)),
        LLMResponse(tool_call=ToolCall("echo", {"try": 2}), usage=_usage(1)),
        LLMResponse(final_text="fixed it", usage=_usage(1)),
    ])
    return run_agent(
        "repair task", llm, make_registry(tmp_path), ColdStore(),
        session_id="golden-repair", max_steps=2,
        repair={"enabled": True, "max_attempts": 2}, **run_kwargs,
    )


SCENARIOS: dict[str, tuple[str, Callable[..., dict]]] = {
    "plain": ("golden-plain", plain_scenario),
    "planner": ("golden-planner", planner_scenario),
    "repair": ("golden-repair", repair_scenario),
}


def normalize(events: list[dict]) -> str:
    lines = []
    for event in events:
        event = {k: v for k, v in event.items() if k not in VOLATILE_KEYS}
        for key in TIMING_KEYS:
            if key in event:
                event[key] = 0
        lines.append(json.dumps(event, sort_keys=True))
    return "\n".join(lines) + "\n"


def run_normalized(name: str, tmp_path: Path, **run_kwargs) -> str:
    session_id, scenario = SCENARIOS[name]
    scenario(tmp_path, **run_kwargs)
    return normalize(read_trace(session_id))


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.jsonl"


def regen_requested() -> bool:
    return os.environ.get("FABRI_REGEN_GOLDENS") == "1"
