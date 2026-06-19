"""The executable side of the agent-as-tool adapter (see agent_tool.py).
Invoked as: python3 agent_runner_tool.py <config_path>, with {"task": "..."}
on stdin -- the same stdin-JSON-in/stdout-JSON-out contract every other tool
uses, so the calling agent's ToolRegistry/runner doesn't need to know this
"tool" is itself a whole agent."""
import json
import sys

from agent_memory.config import load_config
from agent_memory.core.agent import run_agent
from agent_memory.core.outcome import Outcome
from agent_memory.memory.store import QdrantMemoryStore
from agent_memory.runtime import build_llm, build_tool_defs, build_tools


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: agent_runner_tool.py <config_path>"}))
        return 1
    args = json.loads(sys.stdin.read())
    config = load_config(sys.argv[1])

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
    )
    print(json.dumps({"final_text": result["final_text"], "outcome": result["outcome"]}))
    return 0 if result["outcome"] in (Outcome.SUCCESS.value, Outcome.SUCCESS_WITH_RECOVERY.value) else 1


if __name__ == "__main__":
    sys.exit(main())
