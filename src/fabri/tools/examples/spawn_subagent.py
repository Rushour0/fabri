"""Dynamic sub-agent spawn (F1).

The parent agent's runtime *picks* the sub-agent at call time, rather than the
static `tools.agents[]` form where the choice is pre-baked at config load. We
build the same `agent_runner_tool.py` subprocess command the static path does
(see `tools/agent_tool.py:make_agent_tool_manifest`), forward the per-call
overrides as CLI flags, and pipe `{task}` to its stdin -- matching the
stdin-JSON / stdout-JSON contract every other fabri tool uses, so the parent's
runner doesn't need to know this "tool" is itself an entire agent.

Trust boundary: `config_path` and `system_prompt_path` are framework-level
plumbing (yaml + prompt files the host project owns), not user data, so they
are NOT enforced against `$FABRI_SANDBOX_ROOT`. The sub-agent's own
`sandbox_root` (declared in its yaml) still constrains everything *it* reads
or writes. Treat config_path the same way `tools/agent_tool.py` does -- if you
don't trust the parent's argument, don't enable this tool.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

RUNNER_SCRIPT = (
    Path(__file__).resolve().parent.parent / "agent_runner_tool.py"
)

DEFAULT_TIMEOUT_S = 600


def build_runner_command(args: dict, runner_script: Path | None = None) -> list[str]:
    """Pure function so tests can assert flag plumbing without running a real
    sub-agent. Mirrors `make_agent_tool_manifest` from `tools/agent_tool.py`.
    `runner_script=None` reads RUNNER_SCRIPT at call time so tests can monkey-
    patch the module-level constant."""
    script = runner_script if runner_script is not None else RUNNER_SCRIPT
    config_path = str(Path(args["config_path"]).resolve())
    cmd = [sys.executable, str(script), config_path]
    if "system_prompt_inline" in args and "system_prompt_path" in args:
        raise ValueError(
            "spawn_subagent: pass either system_prompt_inline or system_prompt_path, not both"
        )
    if "system_prompt_inline" in args:
        cmd += ["--system-prompt", str(args["system_prompt_inline"])]
    if "system_prompt_path" in args:
        cmd += ["--system-prompt-file", str(Path(args["system_prompt_path"]).resolve())]
    return cmd


def main() -> int:
    raw = sys.stdin.read()
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"malformed JSON on stdin: {e}"}))
        return 1
    if "config_path" not in args or "task" not in args:
        print(json.dumps({"error": "missing required field: config_path or task"}))
        return 1

    try:
        cmd = build_runner_command(args)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 1

    task = args["task"]
    if "additional_context" in args and args["additional_context"]:
        task = f"{args['additional_context']}\n\n{task}"
    runner_stdin = json.dumps({"task": task})

    timeout_s = int(args.get("timeout_s", DEFAULT_TIMEOUT_S))

    try:
        proc = subprocess.run(
            cmd,
            input=runner_stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": f"sub-agent timeout after {timeout_s}s"}))
        return 1
    except OSError as e:
        print(json.dumps({"error": f"failed to spawn sub-agent: {e}"}))
        return 1

    # The runner's stdout is already a JSON object matching this tool's
    # output_schema; pass it through verbatim so the parent's tool_result block
    # gets the sub-agent's {final_text, outcome, session_id, trace_path}.
    stdout = proc.stdout.strip()
    if not stdout:
        stderr_tail = proc.stderr.strip().splitlines()[-5:]
        print(json.dumps({
            "error": "sub-agent produced no stdout",
            "stderr_tail": stderr_tail,
            "returncode": proc.returncode,
        }))
        return 1
    try:
        json.loads(stdout)
    except json.JSONDecodeError as e:
        print(json.dumps({
            "error": f"sub-agent stdout was not JSON: {e}",
            "raw": stdout[:500],
        }))
        return 1
    sys.stdout.write(stdout + "\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
