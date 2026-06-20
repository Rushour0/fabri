"""The executable side of the agent-as-tool adapter (see agent_tool.py).
Invoked as: python3 agent_runner_tool.py <config_path>, with {"task": "..."}
on stdin -- the same stdin-JSON-in/stdout-JSON-out contract every other tool
uses, so the calling agent's ToolRegistry/runner doesn't need to know this
"tool" is itself a whole agent."""
import argparse
import json
import sys

from fabri.config import load_config
from fabri.core.agent import run_agent
from fabri.core.outcome import Outcome
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.traces import trace_path
from fabri.runtime import build_decompose_llm, build_llm, build_tool_defs, build_tools


class _JSONArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 + stderr on bad args; this tool's contract is JSON on
    stdout, exit 1. Override `error` so usage failures stay on-contract."""

    def error(self, message: str) -> None:  # noqa: D401 - argparse override
        print(json.dumps({"error": f"usage: {self.format_usage().strip()} ({message})"}))
        sys.exit(1)


def main() -> int:
    parser = _JSONArgumentParser(prog="agent_runner_tool")
    parser.add_argument("config_path")
    parser.add_argument("--model", default=None, help="Override sub-agent llm.model")
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=None,
                        help="Override sub-agent llm.max_tokens")
    parser.add_argument("--qdrant-url", dest="qdrant_url", default=None,
                        help="Override sub-agent memory.qdrant_url")
    parser.add_argument("--memory-collection", dest="memory_collection", default=None,
                        help="Override sub-agent memory.collection")
    cli_args = parser.parse_args()
    args = json.loads(sys.stdin.read())
    config = load_config(cli_args.config_path)
    if cli_args.model is not None:
        config["llm"]["model"] = cli_args.model
    if cli_args.max_tokens is not None:
        config["llm"]["max_tokens"] = cli_args.max_tokens
    if cli_args.qdrant_url is not None:
        config["memory"]["qdrant_url"] = cli_args.qdrant_url
    if cli_args.memory_collection is not None:
        config["memory"]["collection"] = cli_args.memory_collection

    tools_cfg = config["tools"]
    tools = build_tools(tools_cfg)
    decompose_cfg = tools_cfg["decompose"]
    llm = build_llm(config, build_tool_defs(tools, decompose_cfg))

    mem_cfg = config["memory"]
    store = QdrantMemoryStore(url=mem_cfg["qdrant_url"], collection=mem_cfg["collection"])

    result = run_agent(
        args["task"],
        llm,
        tools,
        store,
        max_steps=config["agent"]["max_steps"],
        top_k=mem_cfg["top_k"],
        max_subquestions=decompose_cfg["max_subquestions"],
        system_prompt=config["agent"].get("system_prompt", ""),
        system_prompt_prefix=config["agent"].get("system_prompt_prefix", ""),
        result_format=tools_cfg.get("result_format", "toon"),
        output_format=config["agent"].get("output_format", "json"),
        decompose_llm=build_decompose_llm(config),
    )
    # Surface the sub-agent's session_id + trace path so a parent agent (or
    # human reading the parent's trace) can pinpoint which JSONL to open when a
    # sub-agent fails. The contract stays additive: existing readers that only
    # look at final_text/outcome are unaffected.
    print(json.dumps({
        "final_text": result["final_text"],
        "outcome": result["outcome"],
        "session_id": result["session_id"],
        "trace_path": str(trace_path(result["session_id"])),
    }))
    return 0 if result["outcome"] in (Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value) else 1


if __name__ == "__main__":
    sys.exit(main())
