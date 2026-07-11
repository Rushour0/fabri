"""Run a fabri agent with an explicit sandbox backend.

fabri's isolation is LAYERED and OPT-IN, chosen programmatically — there is no
`sandbox:` YAML key and no `--sandbox` CLI flag. Every tool call routes through
a `Sandbox` object:

  - LocalSandbox (default) — tools run as ordinary subprocesses, jailed only by
    the `FABRI_SANDBOX_ROOT` path check each file/shell tool enforces. This is
    what `fabri run` and `build_tools()` give you.
  - DockerSandbox (opt-in) — each tool call runs via `docker exec` in a pooled
    container with cap-drop, no-new-privileges, and a pids limit on by default.

This script runs with LocalSandbox as-is (so it works today), and shows the
exact two-line swap to DockerSandbox below.

Run from the repo root:
    pip install 'fabri[sqlite]'
    export GEMINI_API_KEY=...
    python examples/04-docker-sandbox/run_sandboxed.py
"""
import os

from fabri import run_agent
from fabri.config import load_config
from fabri.core.run_config import AgentRunConfig
from fabri.runtime import build_memory_store, build_run_llms, build_tool_defs, build_tools

CONFIG_PATH = "examples/04-docker-sandbox/agent.yaml"
TASK = "List the Python files under src/fabri/memory and say in one line what each does."


def main() -> None:
    config = load_config(CONFIG_PATH)

    # build_tools() returns a registry backed by LocalSandbox, with the config's
    # `enabled` filtering and `sandbox_root` already applied.
    tools = build_tools(config["tools"])

    # ----------------------------------------------------------------------
    # To use REAL container isolation instead, uncomment this block. It swaps
    # the registry's isolation backend and re-points the tool jail root at the
    # container path. Requires a built base image:
    #
    #     docker build -f src/fabri/sandbox/Dockerfile.base -t fabri/sandbox:latest .
    #
    # from fabri.sandbox import DockerSandbox
    # from fabri.sandbox.docker_sandbox import DockerBackend
    #
    # sandbox = DockerSandbox(
    #     image="fabri/sandbox:latest",
    #     pool_size=2,
    #     bind_mounts={os.getcwd(): "/workspace"},   # host project -> container
    #     backend=DockerBackend(
    #         cap_drop_all=True,        # --cap-drop ALL           (default)
    #         no_new_privileges=True,   # --security-opt ...        (default)
    #         pids_limit=512,           # fork-bomb backstop        (default)
    #         mem_limit="512m",         # opt-in: OFF by default (host default)
    #         network="none",           # opt-in: OFF by default (host default)
    #     ),
    # )
    # tools.sandbox = sandbox
    # tools.sandbox_root = "/workspace"   # tools now jail to the container path
    #
    # NOTE: builtin tool manifests resolve their script to an absolute path. For
    # them to run inside the container that path must exist there too — the base
    # image installs fabri for exactly this reason, and host services extend it
    # (`FROM fabri/sandbox:latest` + COPY your tools in). Always `sandbox.dispose()`
    # in a finally block — a leaked container outlives the agent.
    # ----------------------------------------------------------------------

    print(f"sandbox backend: {type(tools.sandbox).__name__}")
    print(f"tool jail root : {tools.sandbox_root}")

    tool_defs = build_tool_defs(tools, config["tools"]["decompose"])
    llms = build_run_llms(config, tool_defs)
    store = build_memory_store(config["memory"])
    run_cfg = AgentRunConfig.from_config(config)

    result = run_agent(
        TASK,
        llms["llm"],
        tools,
        store,
        decompose_llm=llms["decompose_llm"],
        planner_llm=llms["planner_llm"],
        narrator_llm=llms["narrator_llm"],
        **run_cfg.as_kwargs(),
    )
    print("\n--- result ---")
    print(result["final_text"])


if __name__ == "__main__":
    main()
