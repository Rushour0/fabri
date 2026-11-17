"""agent.preflight: tool calls the engine dispatches before the first model turn.

Driven through run_agent with a ScriptedLLMBackend, so the model makes exactly
the calls the script lists. The preflight results must already be in the
messages the first model call sees, and the model must not have to ask for them.
"""
import json
import sys

from fabri import ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.agent import _fill_preflight_input
from fabri.core.llm import LLMResponse
from fabri.core.run_config import AgentRunConfig
from fabri.memory.embedded_store import SqliteMemoryStore
from fabri.tools.manifest_schema import ToolManifest

_ECHO = "import sys,json; a=json.load(sys.stdin); print(json.dumps({'echo': a}))"


class _RecordingBackend(ScriptedLLMBackend):
    def __init__(self, script):
        super().__init__(script)
        self.seen: list[list[dict]] = []

    def step(self, system, messages):
        self.seen.append([dict(m) for m in messages])
        return super().step(system, messages)


def _registry() -> ToolRegistry:
    reg = ToolRegistry([])
    for name in ("context", "specialist"):
        reg.register(ToolManifest(
            name=name, description=name,
            command=[sys.executable, "-c", _ECHO],
            input_schema={}, output_schema={},
        ))
    return reg


def _store(tmp_path):
    return SqliteMemoryStore(path=tmp_path / "memory.db", collection="preflight_test")


def test_preflight_results_reach_the_first_model_call(tmp_path):
    backend = _RecordingBackend([LLMResponse(final_text="done")])
    preflight = (
        [{"tool": "context", "input": {}}, {"tool": "specialist", "input": {"task": "{task}"}}],
        [{"tool": "context", "input": {"note": "lens\n{task}"}}],
    )
    result = run_agent(
        "review this", backend, _registry(), _store(tmp_path), preflight=preflight
    )
    assert result["outcome"] == "success"
    assert len(backend.seen) == 1
    first = backend.seen[0]
    # task, then two (assistant tool_use, user tool_result) pairs.
    assert [m["role"] for m in first] == ["user", "assistant", "user", "assistant", "user"]
    turn_one = first[1]["content"]
    assert [block["name"] for block in turn_one] == ["context", "specialist"]
    assert turn_one[1]["input"] == {"task": "review this"}
    results = first[2]["content"]
    assert [block["tool_use_id"] for block in results] == [b["id"] for b in turn_one]
    assert "review this" in json.dumps(results[1]["content"])
    assert first[3]["content"][0]["input"] == {"note": "lens\nreview this"}


def test_unknown_preflight_tool_is_skipped(tmp_path):
    backend = _RecordingBackend([LLMResponse(final_text="done")])
    result = run_agent(
        "x", backend, _registry(), _store(tmp_path),
        preflight=([{"tool": "missing", "input": {}}],),
    )
    assert result["outcome"] == "success"
    assert [m["role"] for m in backend.seen[0]] == ["user"]


def test_no_preflight_keeps_the_plain_loop(tmp_path):
    backend = _RecordingBackend([LLMResponse(final_text="done")])
    run_agent("x", backend, _registry(), _store(tmp_path))
    assert [m["role"] for m in backend.seen[0]] == ["user"]


def test_fill_replaces_task_in_nested_strings_only():
    filled = _fill_preflight_input({"a": "{task}", "b": ["x {task}", 3], "c": None}, "T")
    assert filled == {"a": "T", "b": ["x T", 3], "c": None}


def test_run_config_carries_preflight_but_not_to_children():
    turns = [[{"tool": "context", "input": {}}]]
    cfg = AgentRunConfig.from_config({"agent": {"preflight": turns}, "tools": {}})
    assert cfg.as_kwargs()["preflight"] == tuple(turns)
    assert cfg.for_subagent(3, None).preflight == ()
