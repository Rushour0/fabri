"""Regression coverage for tools/agent_tool.py's command construction.

The bug this guards against: `make_agent_tool_manifest` used to hardcode the
literal command "python3", resolved from $PATH at subprocess-launch time --
NOT the interpreter fabri itself is running under. Whenever $PATH's `python3`
differs from the interpreter fabri was installed into (pipx installs, Docker
`CMD` without an activated venv, cron, systemd, or simply not having
`source .venv/bin/activate`'d this exact shell), `agent_runner_tool.py`'s very
first import (`from fabri.config import ...`) raised ModuleNotFoundError,
which produced an empty stdout that `runner.py` then reported as the
unhelpful, misleading "malformed JSON output from tool" -- masking the real
cause. Reproduced live and confirmed via a real multi-agent run this session;
see session-notes/plan-agent-tool-python3-bug.md for the full discovery.

`tests/test_unit_agent_runner_tool.py` and `tests/test_spawn_subagent.py`
already test `agent_runner_tool.py` correctly using `[sys.executable, ...]`
directly -- which is exactly why 933+ passing tests never caught this: nothing
asserted that the PRODUCTION path (`make_agent_tool_manifest`) builds a
command using that same, guaranteed-correct interpreter."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from fabri.tools.agent_tool import make_agent_tool_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_make_agent_tool_manifest_uses_current_interpreter_not_bare_python3():
    """The precise regression: command[0] must be sys.executable, an absolute
    path to the interpreter fabri is running under -- not the literal string
    "python3", which resolves from $PATH at launch time and may point
    anywhere."""
    manifest = make_agent_tool_manifest({
        "name": "x", "description": "d", "config": "some/agent.yaml",
    })
    assert manifest.command[0] == sys.executable
    assert manifest.command[0] != "python3"
    assert os.path.isabs(manifest.command[0])


def test_manifest_command_survives_a_path_with_no_working_python3():
    """End-to-end proof, not just a unit assertion: spawn the REAL manifest
    command in a subprocess whose $PATH has no fabri-capable `python3` at
    all -- simulating the exact real-world condition that triggered the
    original bug (a bare "python3" on $PATH resolving to some other, fabri-
    less interpreter). Because sys.executable is an absolute path, it must
    still work regardless of $PATH content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "agent.yaml"
        config_path.write_text(yaml.safe_dump({
            "agent": {"name": "regression-check"},
            "llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "NOT_A_REAL_KEY_VAR"},
            "tools": {"manifest_dir": ["builtin"], "enabled": []},
            "memory": {"backend": "sqlite", "collection": "regression_check",
                       "sqlite_path": str(Path(tmpdir) / "check.db")},
        }))
        manifest = make_agent_tool_manifest({
            "name": "x", "description": "d", "config": str(config_path),
        })

        # A PATH with no working python3 at all (not even a fabri-less one) --
        # the harshest version of the original failure condition. sys.executable
        # is an absolute path, so subprocess.Popen never consults PATH for it.
        env = {**os.environ, "PATH": "/nonexistent-bin-dir"}
        proc = subprocess.run(
            manifest.command, input=json.dumps({"task": "x"}),
            capture_output=True, text=True, env=env, timeout=30,
        )

        # It must not crash with ModuleNotFoundError (the original bug) --
        # `from fabri.config import ...` (agent_runner_tool.py's first line)
        # must succeed regardless of $PATH, proving the command used an
        # absolute-path interpreter rather than a $PATH-resolved "python3".
        assert "ModuleNotFoundError" not in proc.stderr, (
            f"agent_tool's command is still not using the current interpreter; "
            f"stderr: {proc.stderr}"
        )
        # It should get all the way to constructing the OpenAI client and
        # fail there instead, on the missing key -- proof every fabri import
        # in between (config, runtime, core.llm) also succeeded. This one
        # error is NOT caught into the tool's {ok, error} JSON contract today
        # (agent_runner_tool.py has no top-level try/except around
        # build_run_llms) -- a separate, minor, pre-existing gap, not this
        # fix's scope -- so this asserts the real traceback content directly
        # rather than a JSON payload.
        assert "openai.OpenAIError" in proc.stderr or "Missing credentials" in proc.stderr, (
            f"expected a missing-API-key failure, got: {proc.stderr}"
        )
