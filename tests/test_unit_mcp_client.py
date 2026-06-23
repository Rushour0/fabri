"""Unit tests for the MCP stdio client (G19).

We don't spin up a real MCP server — we wire MCPStdioClient up to a pair of
in-memory streams (a fake stdin/stdout) and assert it speaks JSON-RPC + NDJSON
correctly.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from fabri.tools.mcp_client import (
    MCPProtocolError,
    MCPStdioClient,
    _sanitize_name,
)


class _FakeProc:
    """Mimics subprocess.Popen enough for MCPStdioClient to talk to it.

    stdin: a buffer captured for assertion (we never read it back, the "server"
           reacts to it via scripted responses).
    stdout: an io.BytesIO holding the pre-loaded response stream.
    stderr: io.BytesIO so .read() doesn't crash on the error path.
    """
    def __init__(self, responses: list[dict] | list[bytes]):
        self.stdin = io.BytesIO()
        # Allow either dicts (serialized as JSON-NDJSON) or raw bytes (e.g.
        # garbage lines).
        chunks = []
        for r in responses:
            if isinstance(r, bytes):
                chunks.append(r)
            else:
                chunks.append((json.dumps(r) + "\n").encode())
        self.stdout = io.BytesIO(b"".join(chunks))
        self.stderr = io.BytesIO()

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _make_client(responses) -> MCPStdioClient:
    c = MCPStdioClient(command=["unused"], name="test")
    c.proc = _FakeProc(responses)
    return c


def test_request_matches_response_id_and_returns_result():
    c = _make_client([
        {"jsonrpc": "2.0", "id": 1, "result": {"hello": "world"}},
    ])
    assert c._request("ping") == {"hello": "world"}


def test_request_raises_on_jsonrpc_error_payload():
    c = _make_client([
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}},
    ])
    with pytest.raises(MCPProtocolError):
        c._request("bad_method")


def test_request_skips_non_jsonrpc_lines_and_finds_response():
    """A server emitting a startup banner before speaking JSON-RPC must not
    confuse the client — the loop tolerates up to 10 non-JSON lines."""
    c = _make_client([
        b"banner line one\n",
        b"banner line two\n",
        {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
    ])
    assert c._request("anything") == {"ok": True}


def test_request_loops_past_unrelated_notifications():
    """A server may send a `notifications/...` (no id) mid-stream — the client
    must keep reading until the id matches."""
    c = _make_client([
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {"final": True}},
    ])
    assert c._request("real") == {"final": True}


def test_request_raises_on_unexpected_eof():
    """If the server closes mid-call, surface that as MCPProtocolError so the
    registry adapter can convert it to a normal tool_error."""
    c = _make_client([])  # empty stream = EOF immediately
    with pytest.raises(MCPProtocolError):
        c._request("ping")


def test_list_tools_returns_tool_specs():
    c = _make_client([
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": [
            {"name": "echo", "description": "echo back", "inputSchema": {"type": "object"}},
        ]}},
    ])
    tools = c.list_tools()
    assert tools[0]["name"] == "echo"


def test_call_tool_passes_arguments_through():
    """Round-trip: caller sends args, server returns content, client returns
    the raw result."""
    c = _make_client([
        {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
        }},
    ])
    result = c.call_tool("echo", {"x": 1})
    assert result["isError"] is False
    assert result["content"][0]["text"] == "ok"


def test_sanitize_name_strips_specials():
    assert _sanitize_name("foo/bar.baz") == "foo_bar_baz"
    assert _sanitize_name("foo-bar") == "foo_bar"
    assert _sanitize_name("good_name") == "good_name"
