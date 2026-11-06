"""PR #100 review: a stdio MCP child that inherits a FABRI_CRED_* token
through ``credential_env`` must not be able to leak it through a tool result,
a JSON-RPC error or its stderr tail. A real child process echoes the token
on each path."""
from __future__ import annotations

import json
import logging
import sys
import textwrap

import pytest

from fabri.tools.mcp_client import MCPProtocolError, MCPStdioClient, build_mcp_tools

SENTINEL = "sentinel-tok-9f3a7c2e1b"
CRED = "FABRI_CRED_X"

_CHILD = textwrap.dedent(
    """
    import json, os, sys
    tok = os.environ.get(sys.argv[1], "")
    for line in sys.stdin:
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            out = {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05"}}
        elif method == "tools/list":
            out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
                "name": "echo", "description": "echo",
                "inputSchema": {"type": "object", "properties": {"mode": {"type": "string"}}}}]}}
        else:
            mode = (msg.get("params") or {}).get("arguments", {}).get("mode")
            if mode == "result":
                out = {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "token is " + tok}],
                    "structuredContent": {"token": tok}}}
            elif mode == "is_error":
                out = {"jsonrpc": "2.0", "id": mid, "result": {
                    "isError": True, "content": [{"type": "text", "text": "bad " + tok}]}}
            elif mode == "rpc_error":
                out = {"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32000, "message": "denied for " + tok, "data": {"t": tok}}}
            else:
                sys.stderr.write("crashing, my token is " + tok + "\\n")
                sys.stderr.flush()
                sys.exit(3)
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
    """
)


# The env var the child echoes, and the credential_env it is declared with.
# FABRI_CRED_X comes through credential_env; OPENAI_API_KEY is an ordinary
# inherited variable (inherit_env defaults to True) whose name marks it as a
# secret, so it must be redacted although nothing declared it.
_ECHOED = [(CRED, [CRED]), ("OPENAI_API_KEY", [])]


@pytest.fixture(params=_ECHOED, ids=[name for name, _ in _ECHOED])
def server(request, monkeypatch):
    var, credential_env = request.param
    monkeypatch.setenv(var, SENTINEL)
    client, pairs = build_mcp_tools({
        "name": "leaky",
        "command": [sys.executable, "-c", _CHILD, var],
        "credential_env": credential_env,
        "timeout_s": 10,
    })
    handler = dict((m.name, h) for m, h in pairs)["mcp_leaky_echo"]
    yield client, handler
    client.close()


def test_child_really_got_the_token(server):
    client, _ = server
    # The child inherited the value: the raw protocol call carries it.
    raw = client._request("tools/call", {"name": "echo", "arguments": {"mode": "result"}})
    assert SENTINEL in json.dumps(raw)
    assert SENTINEL in client._secrets


def test_result_path_redacts_inherited_token(server):
    _, handler = server
    out = handler({"mode": "result"})
    assert out.get("ok") is True
    assert SENTINEL not in json.dumps(out)
    assert "[redacted]" in json.dumps(out)


def test_is_error_result_redacts_inherited_token(server):
    _, handler = server
    out = handler({"mode": "is_error"})
    assert out.get("ok") is False
    assert SENTINEL not in json.dumps(out)


def test_jsonrpc_error_redacts_inherited_token(server):
    client, handler = server
    with pytest.raises(MCPProtocolError) as exc:
        client.call_tool("echo", {"mode": "rpc_error"})
    assert SENTINEL not in str(exc.value)
    assert "[redacted]" in str(exc.value)
    out = handler({"mode": "rpc_error"})
    assert out.get("ok") is False
    assert SENTINEL not in json.dumps(out)


def test_stderr_tail_redacts_inherited_token(server, caplog):
    _, handler = server
    with caplog.at_level(logging.WARNING, logger="fabri"):
        out = handler({"mode": "crash"})
    assert out.get("ok") is False
    assert SENTINEL not in json.dumps(out)
    assert "crashing, my token is [redacted]" in caplog.text
    assert SENTINEL not in caplog.text


def test_names_not_in_credential_env_are_not_inherited_or_needed(monkeypatch):
    # A FABRI_CRED_* value the server did not ask for is removed, so it is
    # neither passed nor listed as a secret of this client.
    monkeypatch.setenv(CRED, SENTINEL)
    monkeypatch.setenv("FABRI_CRED_OTHER", "other-secret-value-1234")
    c = MCPStdioClient(command=[sys.executable, "-c", "import time; time.sleep(5)"],
                       credential_env=frozenset({CRED}))
    c.start()
    try:
        assert SENTINEL in c._secrets
        assert "other-secret-value-1234" not in c._secrets
    finally:
        c.close()


def test_env_values_still_redacted_before_start():
    c = MCPStdioClient(command=["unused"], env={"T": SENTINEL})
    assert c._secrets == (SENTINEL,)


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                                  "DB_PASSWORD", "STRIPE_KEY", "SMTP_PASS"])
def test_inherited_secret_named_vars_are_redaction_secrets(monkeypatch, name):
    monkeypatch.setenv(name, SENTINEL)
    c = MCPStdioClient(command=[sys.executable, "-c", "import time; time.sleep(5)"])
    c.start()
    try:
        assert SENTINEL in c._secrets
    finally:
        c.close()


def test_short_or_plain_inherited_values_are_not_redaction_secrets(monkeypatch):
    # Short values under a secret name, and long values under a plain name,
    # stay out of the redaction set.
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    monkeypatch.setenv("FABRI_TEST_PLAIN_SETTING", "plain-long-value-123456")
    c = MCPStdioClient(command=[sys.executable, "-c", "import time; time.sleep(5)"])
    c.start()
    try:
        assert "false" not in c._secrets
        assert "plain-long-value-123456" not in c._secrets
    finally:
        c.close()
