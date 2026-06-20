"""End-to-end smoke of the "new user first run" journey: scaffold a project
with `fabri init`, load its agent.yaml, build the registry/llm/store from
runtime helpers (exactly what cli.py does), and drive run_agent through a
ScriptedLLMBackend so it exercises the full real stack -- subprocess tool
runner with sandbox env, trace JSONL writes, outcome classification -- minus
the live API call. If this test breaks, the documented Quickstart broke."""
import json
import os
import uuid
from pathlib import Path

import pytest

from fabri import QdrantMemoryStore, ScriptedLLMBackend, run_agent
from fabri.config import load_config
from fabri.core.llm import LLMResponse, ToolCall
from fabri.runtime import build_tool_defs, build_tools
from fabri.scaffold import scaffold


def _ephemeral_store(name: str) -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=f"smoke_{name}_{uuid.uuid4().hex[:8]}")


def test_first_run_scaffold_then_drive(tmp_path, monkeypatch):
    # Ensure the unrelated sandbox env var from prior tests doesn't leak in --
    # this whole point is to prove the new contract (registry threads it
    # through extra_env) works without it.
    monkeypatch.delenv("FABRI_SANDBOX_ROOT", raising=False)
    # Project-local .fabri/ should land under tmp_path, not the repo cwd.
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))

    # 1. Scaffold the starter project, exactly like `fabri init demo`.
    result = scaffold(str(tmp_path))
    assert "agent.yaml" in result["created"]
    assert (tmp_path / "tools/agent_tools/hello.py").exists()
    assert (tmp_path / ".gitignore").read_text() == ".fabri/\n"

    # 2. Load the scaffolded config and build the same objects cli.py does.
    monkeypatch.chdir(tmp_path)  # tools.manifest_dir/sandbox_root are cwd-relative
    config = load_config(str(tmp_path / "agent.yaml"))
    assert config["llm"]["model"] == "claude-sonnet-4-6"
    assert "hello" in config["tools"]["enabled"]

    tools = build_tools(config["tools"])
    # Sandbox root should be stored on the registry, NOT mutated onto os.environ.
    assert tools.sandbox_root == str(tmp_path.resolve())
    assert "FABRI_SANDBOX_ROOT" not in os.environ
    # Bundled tools resolved via the `builtin` token, plus the scaffolded one.
    assert {"read_file", "write_file", "list_dir", "hello"} <= set(tools.tools)

    # 3. Drive run_agent with a scripted backend: call the custom hello tool,
    # then write its greeting to a sandbox file, then finalize.
    script = [
        LLMResponse(tool_call=ToolCall(name="hello", args={"name": "Ada"}, id="t1")),
        LLMResponse(tool_call=ToolCall(
            name="write_file", args={"path": "out.txt", "content": "Hello, Ada!"}, id="t2",
        )),
        LLMResponse(final_text="greeted Ada and wrote out.txt"),
    ]
    store = _ephemeral_store("first_run")
    out = run_agent(
        "greet Ada with the hello tool",
        ScriptedLLMBackend(script),
        tools,
        store,
        max_steps=config["agent"]["max_steps"],
        result_format=config["tools"].get("result_format", "toon"),
    )

    # 4. Verify the outcome + that the sandbox really wrote the file.
    assert out["outcome"] == "success"
    assert "Ada" in out["final_text"]
    assert (tmp_path / "out.txt").read_text() == "Hello, Ada!"

    # 5. The trace landed under FABRI_HOME/.fabri/traces and is readable JSONL --
    # exercising the file-locked log_event path + read_trace's malformed-line skip.
    trace_file = tmp_path / ".fabri" / "traces" / f"{out['session_id']}.jsonl"
    assert trace_file.exists()
    lines = [json.loads(line) for line in trace_file.read_text().splitlines() if line.strip()]
    types = [e.get("type") for e in lines]
    assert types[0] == "start"
    assert "tool_call" in types
    assert types[-1] == "final"


def test_first_run_incomplete_with_tool_failure(tmp_path, monkeypatch):
    """Sandbox escape from the scaffold + no final_text -> the new outcome
    INCOMPLETE_WITH_TOOL_FAILURE surfaces the real cause (every tool failed)
    instead of being collapsed into a generic INCOMPLETE."""
    monkeypatch.delenv("FABRI_SANDBOX_ROOT", raising=False)
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    scaffold(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    config = load_config(str(tmp_path / "agent.yaml"))
    tools = build_tools(config["tools"])

    # Three failing escape attempts, no final answer -> incomplete_with_tool_failure.
    script = [
        LLMResponse(tool_call=ToolCall(name="read_file", args={"path": "../../etc/passwd"}, id=f"t{i}"))
        for i in range(5)
    ]
    out = run_agent(
        "escape", ScriptedLLMBackend(script), tools, _ephemeral_store("incomplete"), max_steps=3,
    )
    assert out["outcome"] == "incomplete_with_tool_failure"
