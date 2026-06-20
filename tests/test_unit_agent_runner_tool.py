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
