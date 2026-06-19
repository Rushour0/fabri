import json
import subprocess
import time

from agent_memory.core.logging_setup import get_logger
from agent_memory.tools.manifest_schema import ToolManifest

logger = get_logger()


def run_tool(manifest: ToolManifest, args: dict) -> dict:
    """Invoke a tool's command as a subprocess: write `args` as JSON to stdin,
    read a JSON object from stdout. Always returns the normalized shape
    {ok: bool, error?: str, result?: ..., stderr?: str} -- the agent loop never
    sees a raw exception, a hang, or a malformed-JSON crash."""
    logger.debug("tool %s: invoking (args_size=%d bytes)", manifest.name, len(json.dumps(args)))
    t0 = time.monotonic()

    try:
        proc = subprocess.run(
            manifest.command,
            input=json.dumps(args),
            capture_output=True,
            text=True,
            timeout=manifest.timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.warning("tool %s: timeout after %.2fs", manifest.name, time.monotonic() - t0)
        return {"ok": False, "error": f"timeout after {manifest.timeout_s}s"}
    except OSError as e:
        logger.error("tool %s: failed to start: %s", manifest.name, e)
        return {"ok": False, "error": f"failed to start tool: {e}"}

    elapsed = time.monotonic() - t0
    out = {"stderr": proc.stderr} if proc.stderr else {}

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("tool %s: malformed JSON output (%.2fs)", manifest.name, elapsed)
        return {"ok": False, "error": "malformed JSON output from tool", **out}

    if proc.returncode != 0:
        logger.warning("tool %s: exited %d (%.2fs)", manifest.name, proc.returncode, elapsed)
        return {"ok": False, "error": f"tool exited {proc.returncode}", "result": parsed, **out}

    logger.debug("tool %s: ok in %.2fs", manifest.name, elapsed)
    return {"ok": True, "result": parsed, **out}
