"""Unit tests for tools/agent_runner_tool.py -- the script that turns one
agent.yaml into a stdin/stdout subprocess tool another agent can invoke.
These exercise its argv / stdin contract without paying for a live LLM call
by short-circuiting on missing usage / missing key, which both happen before
any LLM client is constructed."""
import json
import os
import subprocess
import sys
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "agent_runner_tool.py"


def _run(args, stdin: str, env_overrides=None):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        input=stdin, capture_output=True, text=True, env=env,
    )


def test_missing_config_arg_returns_error_json():
    proc = _run([], '{"task": "x"}')
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "usage" in payload["error"]


def test_too_many_args_returns_error_json():
    proc = _run(["a", "b"], '{"task": "x"}')
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "usage" in payload["error"]


def test_agent_tool_manifest_threads_model_override():
    """`tools.agents[].model` ends up as `--model X` in the manifest command so
    a parent agent can override the sub-agent's model without editing its yaml."""
    from fabri.tools.agent_tool import make_agent_tool_manifest

    manifest = make_agent_tool_manifest({
        "name": "sub", "description": "d", "config": str(RUNNER),  # any existing path
        "model": "claude-haiku-4-5", "max_tokens": 256,
    })
    cmd = manifest.command
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"
    assert "--max-tokens" in cmd and cmd[cmd.index("--max-tokens") + 1] == "256"


def test_agent_tool_manifest_omits_override_when_absent():
    from fabri.tools.agent_tool import make_agent_tool_manifest

    manifest = make_agent_tool_manifest({"name": "sub", "description": "d", "config": str(RUNNER)})
    assert "--model" not in manifest.command
    assert "--max-tokens" not in manifest.command


# --- B1: sub-agent trace mining -------------------------------------------
# These call main() in-process (rather than via subprocess like the tests
# above) so process_trace can be monkeypatched and asserted on directly.


class _FakeStore:
    pass


def _install_agent_runner_fakes(monkeypatch, *, process_trace_side_effect=None):
    """Stub every heavy dependency main() touches before/around run_agent so
    the test exercises only the new mining wiring, not a real LLM/tool/memory
    stack. Returns the recorder dict the test asserts against."""
    import fabri.tools.agent_runner_tool as art

    calls = {"process_trace": []}

    fake_config = {
        "llm": {}, "memory": {
            "guideline_max_tokens": 100,
            "similarity_threshold": 0.9,
            "promotion_threshold_sessions": 2,
        },
        "tools": {"decompose": {"enabled": False}},
        "agent": {"max_steps": 5},
    }

    monkeypatch.setattr(art, "load_config", lambda path: fake_config)
    monkeypatch.setattr(art, "build_tools", lambda tools_cfg: object())
    monkeypatch.setattr(art, "build_tool_defs", lambda tools, decompose_cfg: [])
    monkeypatch.setattr(
        art, "build_run_llms",
        lambda config, tool_defs: {
            "llm": object(), "decompose_llm": None, "planner_llm": None, "narrator_llm": None,
        },
    )
    fake_store = _FakeStore()
    monkeypatch.setattr(art, "build_memory_store", lambda mem_cfg: fake_store)
    monkeypatch.setattr(art, "build_llm", lambda config, tool_defs: object())

    def fake_run_agent(task, llm, tools, store, **kwargs):
        return {
            "final_text": "done",
            "structured_output": None,
            "outcome": "success",
            "session_id": "child-session-123",
            "usage": None,
        }

    monkeypatch.setattr(art, "run_agent", fake_run_agent)

    def fake_process_trace(session_id, store, llm, **kwargs):
        calls["process_trace"].append((session_id, store, llm))
        if process_trace_side_effect is not None:
            raise process_trace_side_effect
        return []

    monkeypatch.setattr(art, "process_trace", fake_process_trace)
    monkeypatch.setattr(art, "trace_path", lambda session_id: Path(f"/tmp/{session_id}.jsonl"))
    return calls, fake_store


def _run_main(monkeypatch, config_path="cfg.yaml", env=None):
    import fabri.tools.agent_runner_tool as art
    import io

    monkeypatch.setattr(sys, "argv", ["agent_runner_tool", config_path])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"task": "do the thing"}'))
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return art.main()


def test_main_mines_child_session_trace_on_happy_path(monkeypatch):
    calls, fake_store = _install_agent_runner_fakes(monkeypatch)
    rc = _run_main(monkeypatch)
    assert rc == 0
    assert len(calls["process_trace"]) == 1
    session_id, store, llm = calls["process_trace"][0]
    assert session_id == "child-session-123"
    assert store is fake_store


def test_main_skips_mining_when_disabled_via_env(monkeypatch):
    calls, _ = _install_agent_runner_fakes(monkeypatch)
    rc = _run_main(monkeypatch, env={"FABRI_DISABLE_SUBAGENT_MINING": "1"})
    assert rc == 0
    assert calls["process_trace"] == []


def test_main_swallows_process_trace_failure_and_still_succeeds(monkeypatch):
    calls, _ = _install_agent_runner_fakes(
        monkeypatch, process_trace_side_effect=RuntimeError("boom")
    )
    rc = _run_main(monkeypatch)
    assert rc == 0
    assert len(calls["process_trace"]) == 1
