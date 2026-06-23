"""G19: Minimal MCP (Model Context Protocol) client — connect to MCP servers
over stdio and wrap each remote tool as a fabri ToolManifest.

MCP is the emerging standard tool-call protocol used by Claude Code, OpenAI
Agents SDK, Mastra, etc. Supporting it as a *client* widens fabri's available
tool ecosystem without writing a tool per integration — point a config at an
MCP server (filesystem, github, postgres, ...) and its tools land in the
registry.

This implementation is deliberately minimal:
- stdio transport only (HTTP+SSE transport is a follow-up)
- NDJSON framing (line-delimited JSON-RPC 2.0)
- one server per process; the client process is the MCP server's parent

Config shape (`agent.yaml`):
    tools:
      mcp_servers:
        - name: fs                # used as a name prefix (mcp_fs_<tool>)
          command: ["npx", "@modelcontextprotocol/server-filesystem", "/srv/data"]
          env:                    # optional
            FOO: bar

Caveats:
- An MCP server's stdout is JSON-RPC. Any debug output it emits MUST go to
  stderr (it'll otherwise corrupt the framing).
- Errors from the server come back wrapped in the standard fabri tool result
  shape (`{ok: False, error: ...}`), so the agent loop sees no special case.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from fabri.core.logging_setup import get_logger
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_error, tool_ok

logger = get_logger()


class MCPProtocolError(RuntimeError):
    """Raised when the MCP server returns a JSON-RPC error or closes mid-call."""


@dataclass
class MCPStdioClient:
    """A tiny JSON-RPC-over-NDJSON client for a single MCP server process.

    Lifecycle: `start()` → `initialize()` → `list_tools()` / `call_tool(...)` →
    `close()`. Threading: NOT thread-safe — one client serves one consumer
    serially. fabri's agent loop dispatches tools serially within a step, so
    this is fine; if the loop ever calls MCP tools in parallel inside one step
    it'll need a lock.

    Errors: any JSON-RPC error or unexpected EOF surfaces as MCPProtocolError.
    The registry adapter wraps that into a `tool_error` so the agent sees the
    standard `{ok: False, error: ...}` shape.
    """

    command: list[str]
    env: dict[str, str] | None = None
    name: str = "mcp"
    proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)

    def start(self) -> None:
        # text=False so we control encoding explicitly; stderr=PIPE so the
        # server's debug output doesn't pollute the parent terminal but is
        # still visible if a developer wants to drain it.
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            bufsize=0,
        )

    def _write(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        body = (json.dumps(msg) + "\n").encode("utf-8")
        self.proc.stdin.write(body)
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc and self.proc.stdout
        # Skip lines that aren't valid JSON-RPC (some servers emit a one-line
        # banner before they speak protocol; the spec says they shouldn't but
        # in practice servers do).
        for _ in range(10):
            line = self.proc.stdout.readline()
            if not line:
                # EOF — server closed.
                err = (self.proc.stderr.read() or b"").decode("utf-8", errors="replace")
                raise MCPProtocolError(
                    f"MCP server {self.name!r} closed unexpectedly. stderr: {err[-500:]}"
                )
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise MCPProtocolError(
            f"MCP server {self.name!r} sent 10 non-JSON lines before any JSON-RPC"
        )

    def _request(self, method: str, params: Any = None) -> Any:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        # Servers may emit notifications (no `id`) in between request/response;
        # loop until we see the matching id.
        for _ in range(50):
            resp = self._read()
            if resp.get("id") == self._next_id:
                if "error" in resp:
                    raise MCPProtocolError(
                        f"MCP {method} → error: {resp['error']}"
                    )
                return resp.get("result")
        raise MCPProtocolError(
            f"MCP server {self.name!r} sent 50 messages without responding to id={self._next_id}"
        )

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "fabri", "version": "0.7.0"},
            },
        )

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list") or {}
        return result.get("tools", []) or []

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments}) or {}

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _sanitize_name(s: str) -> str:
    """Tool names go into both the LLM tool-call schema and our registry —
    keep them to ascii-alnum-underscore."""
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in s)


def build_mcp_tools(
    server_cfg: dict,
) -> tuple[MCPStdioClient, list[tuple[ToolManifest, "callable"]]]:
    """Connect to one MCP server. Returns the live client and a list of
    (manifest, handler) pairs to register with the ToolRegistry.

    The caller is responsible for `close()`-ing the client when done (the
    registry holds a reference so it stays alive for the agent's lifetime).
    """
    name = server_cfg.get("name") or "mcp"
    command = server_cfg.get("command")
    if not command:
        raise ValueError(f"mcp server {name!r}: missing 'command' (list of str)")
    env = server_cfg.get("env")
    client = MCPStdioClient(command=command, env=env, name=name)
    client.start()
    client.initialize()
    remote_tools = client.list_tools()

    pairs = []
    for spec in remote_tools:
        remote_name = spec.get("name", "unknown")
        fabri_name = f"mcp_{_sanitize_name(name)}_{_sanitize_name(remote_name)}"
        manifest = ToolManifest(
            name=fabri_name,
            description=spec.get("description") or f"MCP tool from {name}: {remote_name}",
            command=[],  # callable-backed; sandbox path is bypassed
            input_schema=spec.get("inputSchema") or {"type": "object"},
            output_schema={"type": "object"},
            timeout_s=float(spec.get("timeoutSeconds") or 30),
        )

        def _handler(args: dict, _remote_name: str = remote_name) -> dict:
            try:
                result = client.call_tool(_remote_name, args)
            except MCPProtocolError as e:
                return tool_error(f"mcp {name}.{_remote_name}: {e}")
            # MCP's tools/call returns {"content": [{...}], "isError": bool}.
            # Surface the raw shape — the agent has enough context to interpret
            # text/image content blocks.
            if result.get("isError"):
                return tool_error(
                    f"mcp {name}.{_remote_name} reported error: {result}"
                )
            return tool_ok(result)

        pairs.append((manifest, _handler))
    return client, pairs
