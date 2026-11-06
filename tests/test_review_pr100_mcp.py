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


# The env var the child echoes, the value it holds, the credential_env it is
# declared with, and the strings that must never come back. FABRI_CRED_X comes
# through credential_env; the others are ordinary inherited variables
# (inherit_env defaults to True) that nothing declared. OPENAI_API_KEY and
# DB_PASSWORD are redacted by name, however short (hunter2 is 7 characters).
# DATABASE_URL is redacted because its value holds user:password, and so is
# the bare password.
DB_URL = "postgresql://app:s3cretpw@db.internal:5432/app"
_ECHOED = [
    (CRED, SENTINEL, [CRED], [SENTINEL]),
    ("OPENAI_API_KEY", SENTINEL, [], [SENTINEL]),
    ("DB_PASSWORD", "hunter2", [], ["hunter2"]),
    ("PGPASSWORD", "12345678", [], ["12345678"]),
    ("TF_TOKEN_app_terraform_io", "87654321", [], ["87654321"]),
    ("REDISCLI_AUTH", "redis-pw-1", [], ["redis-pw-1"]),
    ("DATABASE_URL", DB_URL, [], [DB_URL, "s3cretpw"]),
]


@pytest.fixture(params=_ECHOED, ids=[name for name, *_ in _ECHOED])
def server(request, monkeypatch):
    var, value, credential_env, forbidden = request.param
    monkeypatch.setenv(var, value)
    client, pairs = build_mcp_tools({
        "name": "leaky",
        "command": [sys.executable, "-c", _CHILD, var],
        "credential_env": credential_env,
        "timeout_s": 10,
    })
    handler = dict((m.name, h) for m, h in pairs)["mcp_leaky_echo"]
    yield client, handler, value, forbidden
    client.close()


def _leaks(text: str, forbidden: list[str]) -> list[str]:
    return [f for f in forbidden if f in text]


def test_child_really_got_the_token(server):
    client, _, value, forbidden = server
    # The child inherited the value: the raw protocol call carries it.
    raw = client._request("tools/call", {"name": "echo", "arguments": {"mode": "result"}})
    assert value in json.dumps(raw)
    assert set(forbidden) <= set(client._secrets)


def test_result_path_redacts_inherited_token(server):
    _, handler, _, forbidden = server
    out = handler({"mode": "result"})
    assert out.get("ok") is True
    assert _leaks(json.dumps(out), forbidden) == []
    assert "[redacted]" in json.dumps(out)


def test_is_error_result_redacts_inherited_token(server):
    _, handler, _, forbidden = server
    out = handler({"mode": "is_error"})
    assert out.get("ok") is False
    assert _leaks(json.dumps(out), forbidden) == []


def test_jsonrpc_error_redacts_inherited_token(server):
    client, handler, _, forbidden = server
    with pytest.raises(MCPProtocolError) as exc:
        client.call_tool("echo", {"mode": "rpc_error"})
    assert _leaks(str(exc.value), forbidden) == []
    assert "[redacted]" in str(exc.value)
    out = handler({"mode": "rpc_error"})
    assert out.get("ok") is False
    assert _leaks(json.dumps(out), forbidden) == []


def test_stderr_tail_redacts_inherited_token(server, caplog):
    _, handler, _, forbidden = server
    with caplog.at_level(logging.WARNING, logger="fabri"):
        out = handler({"mode": "crash"})
    assert out.get("ok") is False
    assert _leaks(json.dumps(out), forbidden) == []
    assert "crashing, my token is [redacted]" in caplog.text
    assert _leaks(caplog.text, forbidden) == []


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


def test_flags_numbers_and_plain_inherited_values_are_not_redaction_secrets(monkeypatch):
    # A flag or a number under a secret-looking name is not a credential, and
    # a long value under a plain name is not one either.
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    monkeypatch.setenv("FABRI_TEST_TOKEN_LIMIT", "4096")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("FABRI_TEST_PLAIN_SETTING", "plain-long-value-123456")
    monkeypatch.setenv("FABRI_TEST_HOME_URL", "https://example.com/path")
    c = MCPStdioClient(command=[sys.executable, "-c", "import time; time.sleep(5)"])
    c.start()
    try:
        for value in ("false", "4096", "8080", "plain-long-value-123456", "https://example.com/path"):
            assert value not in c._secrets, value
    finally:
        c.close()


@pytest.mark.parametrize("name,value,expected", [
    # Any name: a URL with a password gives the value and the password.
    ("FABRI_TEST_UPSTREAM", "redis://:p%40ss-word@cache:6379/0", ["redis://:p%40ss-word@cache:6379/0",
                                                                  "p%40ss-word", "p@ss-word"]),
    ("HTTPS_PROXY", "http://u:proxy-pw@proxy:3128", ["http://u:proxy-pw@proxy:3128", "proxy-pw"]),
    # A *_URL / *_DSN value with a user part and no password: the user part
    # is usually a token.
    ("FABRI_TEST_GIT_URL", "https://ghp_tok123456@github.com/o/r.git",
     ["https://ghp_tok123456@github.com/o/r.git", "ghp_tok123456"]),
    ("SENTRY_DSN", "https://abcdef123@o1.ingest.sentry.io/42", ["https://abcdef123@o1.ingest.sentry.io/42",
                                                                "abcdef123"]),
])
def test_inherited_credential_urls_are_redaction_secrets(monkeypatch, name, value, expected):
    monkeypatch.setenv(name, value)
    c = MCPStdioClient(command=[sys.executable, "-c", "import time; time.sleep(5)"])
    c.start()
    try:
        for piece in expected:
            assert piece in c._secrets, piece
    finally:
        c.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_PASSWORD", "12345678"),
        ("DB_PASSWORD", "true"),
        ("GITHUB_TOKEN", "123456"),
        ("STRIPE_KEY", "999999"),
        ("APP_SECRET", "42424242"),
        ("SMTP_PASS", "yes"),
        ("PGPASSWORD", "12345678"),
        ("TF_TOKEN_app_terraform_io", "12345678"),
        ("REDISCLI_AUTH", "1234"),
        ("DOCKER_AUTH_CONFIG", '{"auths":{"r":{"auth":"dXNlcjpwYXNz"}}}'),
        ("COMPOSER_AUTH", '{"github-oauth":{"github.com":"ghp_x123"}}'),
    ],
)
def test_flags_and_numbers_under_strong_secret_names_are_redaction_secrets(name, value):
    from fabri.tools.mcp_client import _inherited_secrets

    assert value in _inherited_secrets(name, value)


@pytest.mark.parametrize(
    ("name", "value"),
    [("MAX_TOKENS", "4096"), ("TOKENIZERS_PARALLELISM", "false"), ("PORT", "8080")],
)
def test_flags_and_numbers_under_loose_names_stay_visible(name, value):
    from fabri.tools.mcp_client import _inherited_secrets

    assert _inherited_secrets(name, value) == []
