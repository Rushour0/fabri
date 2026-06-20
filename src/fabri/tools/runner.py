import json
import os
import signal
import subprocess
import time

from fabri.core.logging_setup import get_logger
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_error, tool_ok

logger = get_logger()


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the tool's whole process group, not just the direct child. Tools
    like bash/python_exec spawn grandchildren; killing only the child (what
    subprocess.run's timeout does) orphans them and leaks processes."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()  # already gone, or no killpg support -- fall back to the child


# Per-tool stdout cap. Even a tool's own `output_max_bytes` is opt-in; a
# misbehaving subprocess streaming gigabytes still needs a hard ceiling so it
# can't OOM the agent or balloon a single tool_result block past the model's
# context. Truncation is signalled in the returned result.
RUNNER_OUTPUT_CAP_BYTES = 1 * 1024 * 1024  # 1 MiB


def run_tool(manifest: ToolManifest, args: dict, extra_env: dict | None = None) -> dict:
    """Invoke a tool's command as a subprocess: write `args` as JSON to stdin,
    read a JSON object from stdout. Always returns the normalized shape
    {ok: bool, error?: str, result?: ..., stderr?: str} -- the agent loop never
    sees a raw exception, a hang, or a malformed-JSON crash.

    `extra_env` is layered on top of os.environ for this one call -- the
    ToolRegistry threads FABRI_SANDBOX_ROOT this way so two concurrent
    registries don't clobber each other's root via the global env."""
    logger.debug("tool %s: invoking (args_size=%d bytes)", manifest.name, len(json.dumps(args)))
    t0 = time.monotonic()

    env = None
    if extra_env is not None:
        env = os.environ.copy()
        env.update(extra_env)

    try:
        # start_new_session=True puts the child in its own process group so a
        # timeout can SIGKILL the entire tree. errors="replace" keeps a tool that
        # emits non-UTF-8 bytes from raising UnicodeDecodeError out of our hands.
        proc = subprocess.Popen(
            manifest.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            env=env,
        )
    except OSError as e:
        logger.error("tool %s: failed to start: %s", manifest.name, e)
        return tool_error(f"failed to start tool: {e}")

    try:
        stdout, stderr = proc.communicate(input=json.dumps(args), timeout=manifest.timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.communicate()  # reap the killed child and close pipes
        logger.warning("tool %s: timeout after %.2fs (killed process group)", manifest.name, time.monotonic() - t0)
        return tool_error(f"timeout after {manifest.timeout_s}s")
    except Exception as e:
        # Broader catch: communicate() can raise on a write to a closed pipe,
        # ValueError on closed fds, etc. Any unexpected runner failure should
        # come back through the normalized contract, not crash the agent loop.
        _kill_process_group(proc)
        logger.error("tool %s: runner failure: %s", manifest.name, e)
        return tool_error(f"runner failure: {e}")

    elapsed = time.monotonic() - t0
    truncated = False
    if len(stdout) > RUNNER_OUTPUT_CAP_BYTES:
        logger.warning(
            "tool %s: stdout %d bytes exceeded cap %d; truncating",
            manifest.name, len(stdout), RUNNER_OUTPUT_CAP_BYTES,
        )
        stdout = stdout[:RUNNER_OUTPUT_CAP_BYTES]
        truncated = True
    out: dict = {"stderr": stderr} if stderr else {}
    if truncated:
        # Truncation almost always also breaks the JSON contract, but flagging
        # it explicitly turns "malformed JSON" into "tool produced too much
        # output" in the trace, which is the real cause.
        out["truncated"] = True

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("tool %s: malformed JSON output (%.2fs)", manifest.name, elapsed)
        return tool_error("malformed JSON output from tool", **out)

    if proc.returncode != 0:
        logger.warning("tool %s: exited %d (%.2fs)", manifest.name, proc.returncode, elapsed)
        return tool_error(f"tool exited {proc.returncode}", result=parsed, **out)

    logger.debug("tool %s: ok in %.2fs", manifest.name, elapsed)
    return tool_ok(parsed, **out)
