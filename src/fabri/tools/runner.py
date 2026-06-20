import json
import os
import signal
import subprocess
import time

from fabri.core.logging_setup import get_logger
from fabri.tools.manifest_schema import ToolManifest

logger = get_logger()


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the tool's whole process group, not just the direct child. Tools
    like bash/python_exec spawn grandchildren; killing only the child (what
    subprocess.run's timeout does) orphans them and leaks processes."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()  # already gone, or no killpg support -- fall back to the child


def run_tool(manifest: ToolManifest, args: dict) -> dict:
    """Invoke a tool's command as a subprocess: write `args` as JSON to stdin,
    read a JSON object from stdout. Always returns the normalized shape
    {ok: bool, error?: str, result?: ..., stderr?: str} -- the agent loop never
    sees a raw exception, a hang, or a malformed-JSON crash."""
    logger.debug("tool %s: invoking (args_size=%d bytes)", manifest.name, len(json.dumps(args)))
    t0 = time.monotonic()

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
        )
    except OSError as e:
        logger.error("tool %s: failed to start: %s", manifest.name, e)
        return {"ok": False, "error": f"failed to start tool: {e}"}

    try:
        stdout, stderr = proc.communicate(input=json.dumps(args), timeout=manifest.timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.communicate()  # reap the killed child and close pipes
        logger.warning("tool %s: timeout after %.2fs (killed process group)", manifest.name, time.monotonic() - t0)
        return {"ok": False, "error": f"timeout after {manifest.timeout_s}s"}

    elapsed = time.monotonic() - t0
    out = {"stderr": stderr} if stderr else {}

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("tool %s: malformed JSON output (%.2fs)", manifest.name, elapsed)
        return {"ok": False, "error": "malformed JSON output from tool", **out}

    if proc.returncode != 0:
        logger.warning("tool %s: exited %d (%.2fs)", manifest.name, proc.returncode, elapsed)
        return {"ok": False, "error": f"tool exited {proc.returncode}", "result": parsed, **out}

    logger.debug("tool %s: ok in %.2fs", manifest.name, elapsed)
    return {"ok": True, "result": parsed, **out}
