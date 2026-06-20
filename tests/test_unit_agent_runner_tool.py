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
