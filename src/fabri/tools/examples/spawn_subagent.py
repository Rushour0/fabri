"""Dynamic sub-agent spawn (F1).

The parent agent's runtime *picks* the sub-agent at call time, rather than the
static `tools.agents[]` form where the choice is pre-baked at config load. We
build the same `agent_runner_tool.py` subprocess command the static path does
(see `tools/agent_tool.py:make_agent_tool_manifest`), forward the per-call
overrides as CLI flags, and pipe `{task}` to its stdin -- matching the
stdin-JSON / stdout-JSON contract every other fabri tool uses, so the parent's
runner doesn't need to know this "tool" is itself an entire agent.

Optional `memory_collection_suffix` namespaces the spawned child's qdrant
collection by suffixing the parent's configured `memory.collection`. Use it
when a multi-domain orchestrator wants each domain child writing guidelines
into its own namespace (e.g. `<parent>_character` vs `<parent>_map`) so
cross-domain retrieval doesn't crowd each child's context.

Trust boundary: `config_path` and `system_prompt_path` are framework-level
plumbing (yaml + prompt files the host project owns), not user data, so they
are NOT enforced against `$FABRI_SANDBOX_ROOT`. The sub-agent's own
`sandbox_root` (declared in its yaml) still constrains everything *it* reads
or writes. Treat config_path the same way `tools/agent_tool.py` does -- if you
don't trust the parent's argument, don't enable this tool.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fabri.core.logging_setup import get_logger

logger = get_logger()

_SUFFIX_MAX_LEN = 32
_SUFFIX_ALLOWED = re.compile(r"[^a-z0-9_-]")

RUNNER_SCRIPT = (
    Path(__file__).resolve().parent.parent / "agent_runner_tool.py"
)

DEFAULT_TIMEOUT_S = 600

# Recursion backstop. A spawned child runs run_agent with a registry that may
# itself include spawn_subagent, so without a cap a confused (or prompt-injected)
# agent can fork-bomb: breadth^depth subprocesses, each with its OWN fresh cost
# budget. We thread the current depth through the env, increment it on every
# spawn, and refuse once it reaches the max. Override the ceiling with
# FABRI_SUBAGENT_MAX_DEPTH.
DEPTH_ENV = "FABRI_SUBAGENT_DEPTH"
MAX_DEPTH_ENV = "FABRI_SUBAGENT_MAX_DEPTH"
DEFAULT_MAX_DEPTH = 5


def _current_depth() -> int:
    try:
        return max(0, int(os.environ.get(DEPTH_ENV, "0")))
    except ValueError:
        return 0


def _max_depth() -> int:
    try:
        return max(0, int(os.environ.get(MAX_DEPTH_ENV, str(DEFAULT_MAX_DEPTH))))
    except ValueError:
        return DEFAULT_MAX_DEPTH


def sanitize_collection_suffix(raw: str) -> str:
    """Qdrant collection names accept a narrow charset; sanitize to lowercase
    `[a-z0-9_-]` and cap at 32 chars so a parent passing `Tile/Map.v2` doesn't
    blow up the spawn with an opaque qdrant error."""
    cleaned = _SUFFIX_ALLOWED.sub("", raw.lower())
    return cleaned[:_SUFFIX_MAX_LEN]


def _parent_collection(config_path: str) -> str | None:
    """Read the parent sub-agent's configured `memory.collection` so the suffix
    can be concatenated. Returns None on any read failure; the caller falls
    back to inheriting the parent collection verbatim."""
    try:
        from fabri.config import load_config

        return load_config(config_path).get("memory", {}).get("collection")
    except Exception as e:  # noqa: BLE001 -- config errors shouldn't kill spawn
        logger.warning("spawn_subagent: could not read parent collection from %s: %s", config_path, e)
        return None


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
    # NOTE: qdrant reachability across the spawn boundary is handled by the
    # `QDRANT_URL` env override in fabri.config.load_config (the child inherits
    # the env), not by forwarding a flag here -- the tool only sees the on-disk
    # config_path, which may carry an unreachable URL.
    raw_suffix = args.get("memory_collection_suffix")
    if raw_suffix:
        suffix = sanitize_collection_suffix(str(raw_suffix))
        if suffix:
            parent = _parent_collection(config_path)
            if parent:
                derived = f"{parent}_{suffix}"
                logger.info("spawn_subagent: routing child memory to %r (suffix=%r)", derived, suffix)
                cmd += ["--memory-collection", derived]
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

    depth = _current_depth()
    max_depth = _max_depth()
    if depth >= max_depth:
        logger.warning("spawn_subagent: refused at depth %d (max %d)", depth, max_depth)
        print(json.dumps({
            "error": (
                f"spawn_subagent refused: recursion depth {depth} reached the limit "
                f"of {max_depth}. Do this subtask inline instead of spawning, or raise "
                f"{MAX_DEPTH_ENV} if deeper nesting is genuinely intended."
            ),
        }))
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

    # Hand the child its depth so ITS spawn_subagent calls (if any) count from
    # here, not from zero. This is what makes the recursion cap actually bound
    # the whole subtree rather than just the immediate parent.
    child_env = os.environ.copy()
    child_env[DEPTH_ENV] = str(depth + 1)

    try:
        proc = subprocess.run(
            cmd,
            input=runner_stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=child_env,
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
