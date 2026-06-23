"""Expose a fabri agent as an MCP server (stdio transport).

Pair to `tools/mcp_client.py`: this lets other MCP-aware runtimes (Claude
Code, OpenAI Agents SDK, Mastra, ...) call a fabri agent as a single tool.

Run it as a module:
    python -m fabri.tools.mcp_server --config agent.yaml [--tool-name fabri_agent]

The MCP server exposes ONE tool, named `fabri_agent` (override via
`--tool-name`), whose `inputSchema` is just `{task: string}`. Calls invoke
`run_agent` with the configured store/tools/llm and return the agent's final
text in the standard MCP content shape (`{content: [{type:"text",text:...}],
isError: false}`).

Transport: NDJSON (line-delimited JSON-RPC 2.0) over stdin/stdout, matching
what `MCPStdioClient` consumes. ANY debug output MUST go to stderr — stdout
is the protocol channel.

Design choices:
- One tool per server. A future version could expose every fabri tool
  individually; one is what makes sense for the "agent-as-a-tool" use case.
- No streaming / partial responses. The MCP server blocks until run_agent
  returns, then sends the final payload. Long runs should bump the caller's
  timeout.
- The MCP server's tool call is treated as a single fabri run; there's no
  multi-turn conversation state held server-side.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from fabri.config import load_config
from fabri.core.agent import run_agent
from fabri.core.logging_setup import configure_logging
from fabri.runtime import (
    build_decompose_llm,
    build_llm,
    build_memory_store,
    build_tool_defs,
    build_tools,
)


def _emit(msg: dict) -> None:
    """Write one JSON-RPC message to stdout, newline-delimited."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _err(_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": _id, "error": {"code": code, "message": message}}


def _ok(_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": _id, "result": result}


class FabriMCPServer:
    def __init__(self, config_path: str | None, tool_name: str = "fabri_agent"):
        self.config = load_config(config_path)
        self.tool_name = tool_name
        self._tools = None
        self._store = None
        self._llm = None
        self._decompose_llm = None

    def _lazy_init(self) -> None:
        """Build the agent's tools/store/llm only on the first tools/call —
        so a client that only does `initialize` + `tools/list` doesn't pay
        the cost of opening Qdrant / loading the LLM SDK."""
        if self._tools is not None:
            return
        mem_cfg = self.config["memory"]
        tools_cfg = self.config["tools"]
        decompose_cfg = tools_cfg["decompose"]
        self._store = build_memory_store(mem_cfg)
        self._tools = build_tools(tools_cfg)
        self._llm = build_llm(self.config, build_tool_defs(self._tools, decompose_cfg))
        self._decompose_llm = build_decompose_llm(self.config)

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        _id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            return _ok(_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fabri", "version": "0.7.2"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None  # one-way notification; no response
        if method == "tools/list":
            return _ok(_id, {
                "tools": [{
                    "name": self.tool_name,
                    "description": (
                        "Run a fabri agent on a task. The agent has access to "
                        "all tools configured in its agent.yaml, plus the "
                        "self-improving memory loop. Returns the agent's final "
                        "answer."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {"task": {"type": "string"}},
                        "required": ["task"],
                    },
                }],
            })
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            task = args.get("task") or ""
            if name != self.tool_name or not task:
                return _err(_id, -32602, f"unknown or malformed tool call: {params!r}")
            try:
                self._lazy_init()
                session_id = str(uuid.uuid4())
                configure_logging(session_id, verbose=False)
                tools_cfg = self.config["tools"]
                decompose_cfg = tools_cfg["decompose"]
                result = run_agent(
                    task, self._llm, self._tools, self._store,
                    session_id=session_id,
                    max_steps=self.config["agent"]["max_steps"],
                    top_k=self.config["memory"]["top_k"],
                    max_subquestions=decompose_cfg["max_subquestions"],
                    system_prompt=self.config["agent"].get("system_prompt", ""),
                    system_prompt_prefix=self.config["agent"].get("system_prompt_prefix", ""),
                    result_format=tools_cfg.get("result_format", "toon"),
                    output_format=self.config["agent"].get("output_format", "json"),
                    decompose_llm=self._decompose_llm,
                    max_cost_usd=self.config["agent"].get("max_cost_usd"),
                )
            except Exception as e:
                return _ok(_id, {
                    "content": [{"type": "text", "text": f"fabri error: {e}"}],
                    "isError": True,
                })
            text = result.get("final_text") or ""
            is_error = not result.get("success")
            return _ok(_id, {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            })
        # Unknown method
        return _err(_id, -32601, f"method not found: {method}")

    def run(self) -> int:
        # Note: stderr is the safe channel for any log/diagnostic. fabri's
        # logging is already routed to stderr by configure_logging.
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _emit(_err(None, -32700, "parse error"))
                continue
            response = self.handle(msg)
            if response is not None:
                _emit(response)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fabri.tools.mcp_server")
    ap.add_argument("--config", default=os.environ.get("FABRI_CONFIG"),
                    help="agent.yaml path. Falls back to FABRI_CONFIG env var.")
    ap.add_argument("--tool-name", default="fabri_agent",
                    help="MCP tool name to expose (default: fabri_agent)")
    args = ap.parse_args(argv)
    return FabriMCPServer(args.config, args.tool_name).run()


if __name__ == "__main__":
    sys.exit(main())
