import json
import os
import signal
import subprocess
import time
from pathlib import Path

from fabri.core.logging_setup import get_logger
from fabri.tools.credential_store import (
    BROKER_ENV_NAMES,
    ENV_CREDENTIAL_PREFIX,
    broker_mode,
    credential_env_var,
    env_without_credentials,
    subagent_credential_names,
)
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


def _subagent_config(manifest: ToolManifest, args: dict) -> str | None:
    """The config path of the sub-agent this tool call starts, or None when the
    tool is not a sub-agent. Two shapes start one: a static ``tools.agents``
    entry runs ``agent_runner_tool.py <config>``, and ``spawn_subagent`` takes
    the config as its ``config_path`` argument."""
    command = [str(part) for part in manifest.command]
    for i, part in enumerate(command[:-1]):
        if Path(part).name == "agent_runner_tool.py":
            return command[i + 1]
    if any(Path(part).name == "spawn_subagent.py" for part in command):
        config = (args or {}).get("config_path")
        return config if isinstance(config, str) and config else None
    return None


def declared_credential_names(manifest: ToolManifest, env: dict | None = None) -> frozenset[str]:
    """The FABRI_CRED_* names in ``env`` (default: this process's) that the
    manifest's ``credentials`` patterns match. An exact handle maps through
    ``credential_env_var``; a "*" handle matches every name that starts with
    the provider's FABRI_CRED_<PROVIDER>_ prefix. No patterns, no names.

    The two credential broker names are never returned, so a manifest that
    declares ``broker:*`` or ``broker:key`` cannot get the broker key. Only a
    sub-agent keeps those (see ``tool_env``)."""
    source = os.environ if env is None else env
    names: set[str] = set()
    for pattern in manifest.credentials:
        provider, handle = pattern.split(":", 1)
        if handle == "*":
            prefix = credential_env_var(provider, "")
            names.update(key for key in source if key.startswith(prefix))
        else:
            names.add(credential_env_var(provider, handle))
    return frozenset(n for n in names if n.startswith(ENV_CREDENTIAL_PREFIX) and n not in BROKER_ENV_NAMES)


def tool_env(manifest: ToolManifest, args: dict, extra_env: dict | None = None) -> dict:
    """The environment one tool subprocess gets: this process's env with every
    FABRI_CRED_* variable removed, except the ones the manifest's ``credentials``
    patterns declare, then ``extra_env``.

    A sub-agent always keeps the two credential broker names when the run has
    them, whatever its config says, because the broker answers it from the
    parent run's own binding. Without a broker (the snapshot path) it keeps the
    credentials its own config declares and its policy grants, so its sources
    still work. Every other tool, bash and python_exec included, sees no token
    and no broker name at all."""
    keep: frozenset[str] = frozenset()
    keep_broker = False
    config = _subagent_config(manifest, args)
    if config is not None:
        # Both broker names or neither: a lone one is not a broker run.
        keep_broker = broker_mode()
        if not keep_broker:
            try:
                keep = subagent_credential_names(config, actor=os.environ.get("FABRI_RUN_ACTOR") or None)
            except Exception as e:  # noqa: BLE001 -- an unreadable config passes no credentials
                logger.warning("tool %s: sub-agent credentials not passed (%s)", manifest.name, type(e).__name__)
    env = env_without_credentials(keep=keep | declared_credential_names(manifest), keep_broker=keep_broker)
    if extra_env:
        env.update(extra_env)
    return env


def run_tool(manifest: ToolManifest, args: dict, extra_env: dict | None = None) -> dict:
    """Invoke a tool's command as a subprocess: write `args` as JSON to stdin,
    read a JSON object from stdout. Always returns the normalized shape
    {ok: bool, error?: str, result?: ..., stderr?: str} -- the agent loop never
    sees a raw exception, a hang, or a malformed-JSON crash.

    `extra_env` is layered on top of os.environ for this one call -- the
    ToolRegistry threads FABRI_SANDBOX_ROOT this way so two concurrent
    registries don't clobber each other's root via the global env. Connector
    credentials (FABRI_CRED_*) are removed first, see `tool_env`."""
    logger.debug("tool %s: invoking (args_size=%d bytes)", manifest.name, len(json.dumps(args)))
    t0 = time.monotonic()

    env = tool_env(manifest, args, extra_env)

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
