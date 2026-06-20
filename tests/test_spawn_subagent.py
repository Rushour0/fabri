"""F1 -- dynamic spawn_subagent. Three layers of coverage:

1. `build_runner_command` pure-function unit tests: flag plumbing matches
   the static F0 path (no live runner).
2. The tool subprocess wired through a fake `agent_runner_tool.py` (env
   override) so we can verify stdin/stdout/argv contract end-to-end without
   paying for an LLM call.
3. Manifest registration: `ToolRegistry` picks up spawn_subagent.json from
   the bundled `builtin` examples dir.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

EXAMPLES_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"
)
TOOL_SCRIPT = EXAMPLES_DIR / "spawn_subagent.py"


def _run_tool(stdin: str, fake_runner: Path | None = None):
    """Invoke spawn_subagent.py exactly as the runner would: argv-less, JSON on
    stdin. If `fake_runner` is set we point RUNNER_SCRIPT at it via a
    monkey-patched import so we never touch a real sub-runner."""
    env = os.environ.copy()
    extra = ""
    if fake_runner is not None:
        # Inject a tiny shim that overrides RUNNER_SCRIPT before main() runs.
        extra = textwrap.dedent(
            f"""
            import fabri.tools.examples.spawn_subagent as s
            from pathlib import Path
            s.RUNNER_SCRIPT = Path({str(fake_runner)!r})
            import sys as _sys
            _sys.exit(s.main())
            """
        )
        return subprocess.run(
            [sys.executable, "-c", extra],
            input=stdin, capture_output=True, text=True, env=env,
        )
    return subprocess.run(
        [sys.executable, str(TOOL_SCRIPT)],
        input=stdin, capture_output=True, text=True, env=env,
    )


def _make_fake_runner(tmp_path: Path, body: str) -> Path:
    """Write a tiny python script that pretends to be agent_runner_tool.py:
    parses argv, reads stdin, prints whatever `body` returns as JSON."""
    p = tmp_path / "fake_runner.py"
    p.write_text(textwrap.dedent(body))
    return p


def test_build_runner_command_threads_system_prompt_inline():
    from fabri.tools.examples.spawn_subagent import build_runner_command

    cmd = build_runner_command(
        {"config_path": "/tmp/x.yaml", "task": "t", "system_prompt_inline": "be terse"}
    )
    assert "--system-prompt" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "be terse"
    assert "--system-prompt-file" not in cmd


def test_build_runner_command_threads_system_prompt_file(tmp_path):
    from fabri.tools.examples.spawn_subagent import build_runner_command

    f = tmp_path / "p.md"
    f.write_text("you are a tester")
    cmd = build_runner_command(
        {"config_path": "/tmp/x.yaml", "task": "t", "system_prompt_path": str(f)}
    )
    assert "--system-prompt-file" in cmd
    assert cmd[cmd.index("--system-prompt-file") + 1] == str(f.resolve())
    assert "--system-prompt" not in cmd


def test_build_runner_command_rejects_both_prompt_forms():
    from fabri.tools.examples.spawn_subagent import build_runner_command

    with pytest.raises(ValueError):
        build_runner_command({
            "config_path": "/tmp/x.yaml", "task": "t",
            "system_prompt_inline": "a", "system_prompt_path": "/tmp/p.md",
        })


def test_missing_required_field_returns_error_json(tmp_path):
    proc = _run_tool(json.dumps({"config_path": "x"}))  # missing task
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "missing required field" in payload["error"]


def test_additional_context_prepended_to_task(tmp_path):
    """Fake runner echoes the stdin it received as final_text; we assert
    additional_context landed before the task body."""
    runner = _make_fake_runner(tmp_path, """
        import json, sys
        args = json.loads(sys.stdin.read())
        print(json.dumps({
            "final_text": args["task"],
            "outcome": "success",
            "session_id": "fake",
            "trace_path": "/tmp/fake.jsonl",
        }))
    """)
    fake_cfg = tmp_path / "agent.yaml"
    fake_cfg.write_text("# unused, our fake runner ignores it\n")

    proc = _run_tool(json.dumps({
        "config_path": str(fake_cfg),
        "task": "build the village",
        "additional_context": "prior step shipped tiles v3",
    }), fake_runner=runner)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["final_text"] == "prior step shipped tiles v3\n\nbuild the village"
    assert payload["outcome"] == "success"
    assert payload["session_id"] == "fake"


def test_passes_through_runner_stdout_verbatim(tmp_path):
    runner = _make_fake_runner(tmp_path, """
        import json
        print(json.dumps({
            "final_text": "ok",
            "outcome": "success",
            "session_id": "abc",
            "trace_path": "/tmp/abc.jsonl",
        }))
    """)
    fake_cfg = tmp_path / "agent.yaml"
    fake_cfg.write_text("# unused\n")

    proc = _run_tool(
        json.dumps({"config_path": str(fake_cfg), "task": "hi"}),
        fake_runner=runner,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload == {
        "final_text": "ok", "outcome": "success",
        "session_id": "abc", "trace_path": "/tmp/abc.jsonl",
    }


def test_sub_agent_empty_stdout_returns_error(tmp_path):
    runner = _make_fake_runner(tmp_path, """
        import sys
        print("boom", file=sys.stderr)
        sys.exit(2)
    """)
    fake_cfg = tmp_path / "agent.yaml"
    fake_cfg.write_text("# unused\n")

    proc = _run_tool(
        json.dumps({"config_path": str(fake_cfg), "task": "hi"}),
        fake_runner=runner,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "no stdout" in payload["error"]


def test_spawn_subagent_registered_as_builtin():
    """The bundled `builtin` manifest_dir resolves to a registry that
    includes spawn_subagent."""
    from fabri.tools.registry import ToolRegistry

    reg = ToolRegistry(EXAMPLES_DIR)
    assert "spawn_subagent" in reg.tools
    m = reg.tools["spawn_subagent"]
    assert "config_path" in m.input_schema["properties"]
    assert "parallel_group" in m.input_schema["properties"]
