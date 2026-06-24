"""Unit tests for v0.7.5:

- 1a: terminal INCOMPLETE / FAILED events carry the model's last assistant
  text so a host doesn't have to scrape `thought` events to render a recap.
- 1b: on the LAST allowed step, the loop injects a one-shot "stop calling
  tools, answer now" nudge into the last user message — converting the
  "did-the-work-ran-out-of-narration-budget" case into a clean SUCCESS.
- Item 2: agent.subagent.{max_steps,max_cost_usd} overrides the parent
  budget for spawned children only; absent => fall back to parent values.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

from fabri import QdrantMemoryStore, ScriptedLLMBackend, ToolRegistry, run_agent
from fabri.core.llm import LLMResponse, ToolCall
from fabri.orchestrator.traces import read_trace
from fabri.tools.manifest_schema import ToolManifest


RUNNER = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "agent_runner_tool.py"


def _store():
    return QdrantMemoryStore(collection=f"v075_{uuid.uuid4().hex[:8]}")


def _noop_registry():
    reg = ToolRegistry([])
    reg.register(ToolManifest(
        name="noop", description="ok",
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print(json.dumps({}))"],
        input_schema={}, output_schema={},
    ))
    return reg


# ----- 1a -----

def test_incomplete_event_carries_last_assistant_text():
    """When a run hits max_steps without a final, the terminal INCOMPLETE
    event MUST include the model's last assistant text (thinking_text from
    the prior step) as `text`. Hosts otherwise have nothing to show."""
    reg = _noop_registry()
    script = [
        LLMResponse(
            tool_call=ToolCall(name="noop", args={}, id="t1"),
            thinking_text="Step 1 plan: call noop to warm things up.",
        ),
        # Last allowed step: nudge fires; model still chooses to call a tool
        # and we run out — but the thinking_text is the recap we want surfaced.
        LLMResponse(
            tool_call=ToolCall(name="noop", args={}, id="t2"),
            thinking_text="Wrapping up: did the work, summarising now.",
        ),
    ]
    sid = f"test_1a_{uuid.uuid4().hex[:8]}"
    r = run_agent("x", ScriptedLLMBackend(script), reg, _store(),
                  session_id=sid, max_steps=2)
    assert r["outcome"] == "incomplete"
    events = read_trace(sid)
    terminal = next(e for e in events if e.get("type") == "incomplete")
    assert terminal.get("text") == "Wrapping up: did the work, summarising now."
    # `reason`/`outcome` still present (back-compat)
    assert terminal.get("reason") == "max steps reached"
    assert terminal.get("outcome") == "incomplete"


def test_failed_event_carries_last_assistant_text():
    """FAILED terminal events also surface last assistant text."""
    from fabri.core.llm import LLMError

    class _DyingBackend:
        def __init__(self):
            self._i = 0

        def set_tools(self, _):
            return None

        def prewarm(self, _):
            return None

        def step(self, system, messages):
            self._i += 1
            if self._i == 1:
                return LLMResponse(
                    tool_call=ToolCall(name="noop", args={}, id="t1"),
                    thinking_text="One thought before things go south.",
                )
            raise LLMError("provider exploded")

    sid = f"test_1a_failed_{uuid.uuid4().hex[:8]}"
    r = run_agent("x", _DyingBackend(), _noop_registry(), _store(),
                  session_id=sid, max_steps=5)
    assert r["outcome"] == "failed"
    events = read_trace(sid)
    terminal = next(e for e in events if e.get("type") == "failed")
    assert terminal.get("text") == "One thought before things go south."
    assert terminal.get("reason") == "provider exploded"


# ----- 1b -----

def test_final_step_nudge_converts_max_steps_into_success():
    """The orchestrator injects a 'this is your final step' instruction on
    the last allowed step. A model that respects it emits final_text and the
    run ends `success` instead of `incomplete`."""
    captured: dict[str, list] = {"last_messages": None}

    class _NudgeAwareBackend:
        def __init__(self):
            self._i = 0

        def set_tools(self, _):
            return None

        def prewarm(self, _):
            return None

        def step(self, system, messages):
            self._i += 1
            # Snapshot what the loop sent us on the LAST step.
            if self._i == 2:
                captured["last_messages"] = messages
                # Model "obeys" the nudge.
                return LLMResponse(final_text="done — work shipped")
            # First step: keep working with a tool call.
            return LLMResponse(tool_call=ToolCall(name="noop", args={}, id="t1"))

    sid = f"test_1b_{uuid.uuid4().hex[:8]}"
    r = run_agent("x", _NudgeAwareBackend(), _noop_registry(), _store(),
                  session_id=sid, max_steps=2)
    assert r["outcome"] == "success"
    assert r["final_text"] == "done — work shipped"

    # Verify the nudge was actually appended to the last user message.
    msgs = captured["last_messages"]
    assert msgs and msgs[-1]["role"] == "user"
    content = msgs[-1]["content"]
    flat = content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in content if isinstance(b, dict)
    )
    assert "FINAL step" in flat


def test_final_step_nudge_not_injected_when_max_steps_one():
    """max_steps=1 means there's no prior step the model could have made
    tool calls on; the nudge would push the model away from doing anything
    useful, so it must NOT fire. Verifies the gate on max_steps > 1."""
    captured: dict[str, list] = {"messages": None}

    class _Recorder:
        def set_tools(self, _):
            return None

        def prewarm(self, _):
            return None

        def step(self, system, messages):
            captured["messages"] = [dict(m) for m in messages]
            return LLMResponse(final_text="single shot")

    r = run_agent("x", _Recorder(), ToolRegistry([]), _store(), max_steps=1)
    assert r["outcome"] == "success"
    msgs = captured["messages"]
    flat = msgs[-1]["content"] if isinstance(msgs[-1]["content"], str) else ""
    assert "FINAL step" not in flat


# ----- Item 2: subagent budget override -----

def _stub_runner_runtime(monkeypatch, capture: dict):
    """Patch the runner so main() doesn't try to build a real LLM/Qdrant
    store; capture the run_agent kwargs instead."""
    from fabri.tools import agent_runner_tool as art

    def fake_run_agent(task, llm, tools, store, **kwargs):
        capture["kwargs"] = kwargs
        capture["task"] = task
        return {
            "session_id": "stub",
            "success": True,
            "final_text": "ok",
            "outcome": "success",
            "usage": {"cost_usd": 0.0, "total_cost_usd": 0.0, "step_count": 1, "wall_time_s": 0.0},
        }

    monkeypatch.setattr(art, "run_agent", fake_run_agent)
    monkeypatch.setattr(art, "build_tools", lambda cfg: ToolRegistry([]))
    monkeypatch.setattr(art, "build_tool_defs", lambda tools, dc: [])
    monkeypatch.setattr(art, "build_run_llms", lambda cfg, defs: {
        "llm": object(), "decompose_llm": None, "planner_llm": None, "narrator_llm": None,
    })

    class _StubStore:
        def __init__(self, **_):
            pass

    monkeypatch.setattr(art, "QdrantMemoryStore", _StubStore)


def _runner_main_with_config(cfg_dict: dict, monkeypatch, tmp_path) -> dict:
    """Invoke agent_runner_tool.main() against a synthetic config and return
    the kwargs the stubbed run_agent saw."""
    import yaml as _yaml

    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text(_yaml.safe_dump(cfg_dict))

    capture: dict = {}
    _stub_runner_runtime(monkeypatch, capture)

    from fabri.tools import agent_runner_tool as art

    monkeypatch.setattr(sys, "argv", ["agent_runner_tool", str(cfg_path)])
    monkeypatch.setattr(sys, "stdin", _StringIO('{"task": "do it"}'))

    rc = art.main()
    assert rc == 0, "stub should report success"
    return capture["kwargs"]


class _StringIO:
    """argparse uses sys.stdin.read(); a real StringIO works."""

    def __init__(self, text):
        from io import StringIO as _S
        self._s = _S(text)

    def read(self, *a, **kw):
        return self._s.read(*a, **kw)


def test_subagent_block_overrides_parent_max_steps(monkeypatch, tmp_path):
    """When agent.subagent.max_steps is set, the runner passes IT (not the
    parent's max_steps) to run_agent. Same for max_cost_usd."""
    cfg = {
        "agent": {
            "name": "child",
            "max_steps": 50,            # parent's inflated budget
            "max_cost_usd": 5.0,
            "subagent": {
                "max_steps": 8,         # children stay tight
                "max_cost_usd": 0.50,
            },
        },
    }
    kwargs = _runner_main_with_config(cfg, monkeypatch, tmp_path)
    assert kwargs["max_steps"] == 8
    assert kwargs["max_cost_usd"] == 0.50


def test_subagent_block_absent_inherits_parent(monkeypatch, tmp_path):
    """No subagent block (or all-None values) keeps the historical behaviour:
    children inherit agent.max_steps and agent.max_cost_usd verbatim."""
    cfg = {
        "agent": {
            "name": "child",
            "max_steps": 25,
            "max_cost_usd": 1.0,
        },
    }
    kwargs = _runner_main_with_config(cfg, monkeypatch, tmp_path)
    assert kwargs["max_steps"] == 25
    assert kwargs["max_cost_usd"] == 1.0


def test_subagent_partial_override(monkeypatch, tmp_path):
    """Each field falls back independently: a subagent block with only
    max_steps set leaves max_cost_usd inheriting the parent value."""
    cfg = {
        "agent": {
            "name": "child",
            "max_steps": 50,
            "max_cost_usd": 3.0,
            "subagent": {"max_steps": 5},
        },
    }
    kwargs = _runner_main_with_config(cfg, monkeypatch, tmp_path)
    assert kwargs["max_steps"] == 5
    assert kwargs["max_cost_usd"] == 3.0
