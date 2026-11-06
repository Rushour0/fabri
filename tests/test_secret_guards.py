"""Guards that no credential value shows up where it should not: logs, reprs,
CLI output, error text, list output, file modes and run environments."""
from __future__ import annotations

import json
import logging
import os
import socket
import stat
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml

from fabri.cli import main
from fabri.service.connection_store import ConnectionStore
from fabri.service.launcher import launch_run
from fabri.tools.connections.cakemail_login import CAKEMAIL_PATS_URL, CAKEMAIL_TOKEN_URL, pat_scopes
from fabri.tools.mcp_client import MCPHttpClient, MCPProtocolError
from fabri.tools.sources import build_source_tools

pytestmark = pytest.mark.unit

ACCESS = "acc-guard-9f3a1c77e0d24b"
REFRESH = "ref-guard-5d8b2e41a6c93f"
ROTATED = "rot-guard-2b7c4d90e1f85a"
TOKEN = "ck_pat_" + "c3" * 20
PAT = "ck_pat_" + "7a" * 20
PASSWORD = "pw-Guard-Horse-unique"
LOGIN_ACCESS = "jwt-guard-login-unique"
VERIFY_URL = "https://api.cakemail.dev/users/self"
SENSITIVE_KEY_PARTS = ("token", "secret", "password")


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def walk_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from walk_keys(inner)


def assert_clean(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text


class FakeStdin:
    def __init__(self, text: str = "", tty: bool = False) -> None:
        import io

        self._buf = io.StringIO(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def __getattr__(self, name):
        return getattr(self._buf, name)

    def __iter__(self):
        return iter(self._buf)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("FABRI_INSTALL_DB", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                      ("93.184.216.34", 0))])


@pytest.fixture
def cakemail(monkeypatch):
    """A fake Cakemail API on the connect transport hook. Error bodies echo the
    Authorization header, so a leak through an error path would show."""
    state = {"verify_status": 200, "valid": {TOKEN, PAT}}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == VERIFY_URL and request.method == "GET":
            auth = request.headers.get("authorization", "")
            if state["verify_status"] != 200:
                return httpx.Response(state["verify_status"], json={"detail": f"echo {auth}"})
            if auth.removeprefix("Bearer ") not in state["valid"]:
                return httpx.Response(401, json={"detail": f"echo {auth}"})
            return httpx.Response(200, json={"data": {"id": 1, "email": "ops@example.com"}})
        if url == CAKEMAIL_TOKEN_URL:
            return httpx.Response(200, json={"access_token": LOGIN_ACCESS, "expires_in": 3600, "accounts": [7]})
        if url == CAKEMAIL_PATS_URL:
            return httpx.Response(200, json={"key_prefix": PAT[:12], "name": "n", "scopes": list(pat_scopes()),
                                             "status": "active", "created_at": 1, "plaintext": PAT})
        return httpx.Response(404)

    monkeypatch.setattr("fabri.tools.connections._transport", httpx.MockTransport(handler))
    return state


def run(monkeypatch, *argv, stdin: FakeStdin | None = None) -> int:
    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", stdin)
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


# ------------------------------------------------------------------ store


def test_connection_store_logs_and_reprs_carry_no_token(tmp_path, caplog):
    secrets = (ACCESS, REFRESH, ROTATED)
    with caplog.at_level(logging.DEBUG):
        store = ConnectionStore(tmp_path / "installs.db")
        store.upsert(provider="linear", handle="ws1", access_token=ACCESS, refresh_token=REFRESH,
                     expires_in=100, display_name="Acme", now=1_000.0)

        def failing(refresh_token):
            raise OSError(f"network down {refresh_token}")

        store.refreshers["linear"] = failing
        # Near expiry: the refresher fails, the live token is still handed out.
        assert store.get_token("linear", "ws1", now=1_090.0) == ACCESS
        # Past expiry: the dead token is dropped.
        assert store.get_token("linear", "ws1", now=1_200.0) is None
        store.upsert(provider="linear", handle="ws1", access_token=ROTATED, refresh_token=REFRESH,
                     expires_in=100, now=2_000.0)
        assert store.delete("linear", "ws1") is True
    assert_clean(caplog.text, *secrets)
    assert_clean(repr(store) + str(store), *secrets)
    for row in (store.list() or []):
        assert_clean(repr(row), *secrets)


# -------------------------------------------------------------------- CLI


def test_cli_connect_outputs_carry_no_token(tmp_path, monkeypatch, capsys, caplog, cakemail):
    caplog.set_level(logging.DEBUG)
    home = str(tmp_path)
    secrets = (TOKEN, PAT, PASSWORD, LOGIN_ACCESS)
    assert run(monkeypatch, "connect", "cakemail", "--token-stdin", "--home-root", home,
               stdin=FakeStdin(TOKEN)) == 0
    # Replace the row.
    assert run(monkeypatch, "connect", "cakemail", "--token-stdin", "--home-root", home,
               stdin=FakeStdin(TOKEN)) == 0
    # A refused token: the fake API echoes the Authorization header in its body.
    cakemail["verify_status"] = 401
    assert run(monkeypatch, "connect", "cakemail", "--handle", "bad", "--token-stdin", "--home-root", home,
               stdin=FakeStdin(TOKEN)) == 1
    cakemail["verify_status"] = 500
    assert run(monkeypatch, "connect", "cakemail", "--handle", "bad", "--token-stdin", "--home-root", home,
               stdin=FakeStdin(TOKEN)) == 1
    cakemail["verify_status"] = 200
    # No TTY and no flag.
    assert run(monkeypatch, "connect", "cakemail", "--home-root", home, stdin=FakeStdin(TOKEN)) == 2
    # list, json list, remove (twice).
    assert run(monkeypatch, "connections", "list", "--home-root", home) == 0
    assert run(monkeypatch, "connections", "list", "--json", "--home-root", home) == 0
    assert run(monkeypatch, "connections", "remove", "cakemail", "main", "--home-root", home) == 0
    assert run(monkeypatch, "connections", "remove", "cakemail", "main", "--home-root", home) == 1
    out, err = capsys.readouterr()
    assert_clean(out + err + caplog.text, *secrets)


def test_cli_login_outputs_carry_no_password_or_token(tmp_path, monkeypatch, capsys, caplog, cakemail):
    caplog.set_level(logging.DEBUG)
    answers = iter(["ops@example.com", PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="Password: ": next(answers))
    monkeypatch.setattr("builtins.input", lambda prompt="": "ops@example.com")
    code = run(monkeypatch, "connect", "cakemail", "--login", "--home-root", str(tmp_path),
               stdin=FakeStdin("", tty=True))
    out, err = capsys.readouterr()
    assert code == 0, (out, err)
    assert_clean(out + err + caplog.text, PAT, PASSWORD, LOGIN_ACCESS)
    s = ConnectionStore(tmp_path / "installs.db")
    assert s.get_token("cakemail", "main") == PAT
    assert_clean(json.dumps(s.list()), PAT, PASSWORD, LOGIN_ACCESS)


# ------------------------------------------------------------- 401 errors


def test_http_401_hint_carries_no_header_value(caplog):
    caplog.set_level(logging.DEBUG)
    token = "pat_guard_0123456789abcdef"

    class Store:
        def get(self, provider, handle):
            return token

    def handle(request):
        return httpx.Response(401, json={"detail": "invalid token"})

    tool = build_source_tools(
        {"kind": "http", "name": "ck_lists", "url": "https://api.example.test/lists", "method": "GET",
         "headers": {"Authorization": {"secret_ref": "cakemail:main", "prefix": "Bearer "}}},
        transport=httpx.MockTransport(handle), store=Store())[0]
    out = tool.handler({})
    assert out["ok"] is False and "run fabri connect cakemail" in out["error"]
    # The hint and the logs never carry the value. (The upstream body is passed
    # through map_response untouched, so this mock does not echo the header.)
    assert_clean(out["error"] + caplog.text, token)


class Rotating:
    def __init__(self) -> None:
        self.calls = 0

    def token(self, n: int) -> str:
        return f"rotated_guard_value_{n:04d}"

    def __call__(self):
        self.calls += 1
        return {"Authorization": f"Bearer {self.token(self.calls)}"}


def test_mcp_401_error_carries_no_header_value(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FABRI_FETCH_ALLOW_PRIVATE", "1")
    source = Rotating()

    def handler(request):
        return httpx.Response(401, text=f"refused {request.headers.get('Authorization')}")

    hint = "the stored cakemail credential was refused, run fabri connect cakemail"
    client = MCPHttpClient(url="http://127.0.0.1:9/mcp", header_source=source, credential_hint=hint,
                           headers={"X-Static": "static_guard_value"},
                           transport=httpx.MockTransport(handler))
    with pytest.raises(MCPProtocolError) as exc:
        client.list_tools()
    client.close()
    text = str(exc.value) + repr(exc.value) + caplog.text
    for n in range(1, source.calls + 1):
        assert source.token(n) not in text
    assert "static_guard_value" not in text
    assert hint in str(exc.value)


# --------------------------------------------------------- list output / modes


def test_connections_list_json_has_no_token_keys(tmp_path, monkeypatch, capsys, cakemail):
    home = str(tmp_path)
    assert run(monkeypatch, "connect", "cakemail", "--token-stdin", "--home-root", home,
               stdin=FakeStdin(TOKEN)) == 0
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(provider="linear", handle="ws1", access_token=ACCESS, refresh_token=REFRESH,
                 expires_in=3600, display_name="Acme")
    capsys.readouterr()
    assert run(monkeypatch, "connections", "list", "--json", "--home-root", home) == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert len(rows) == 2
    for key in walk_keys(rows):
        assert not any(part in key.lower() for part in SENSITIVE_KEY_PARTS), key
    assert_clean(out, TOKEN, ACCESS, REFRESH)
    for row in store.list():
        for key in walk_keys(row):
            assert not any(part in key.lower() for part in SENSITIVE_KEY_PARTS), key
    for key in walk_keys(store.get("linear", "ws1")):
        assert not any(part in key.lower() for part in SENSITIVE_KEY_PARTS), key


def test_installs_db_mode_is_0600_after_every_cli_command(tmp_path, monkeypatch, capsys, cakemail):
    home = str(tmp_path)
    db = tmp_path / "installs.db"
    commands = [
        (("connect", "cakemail", "--token-stdin", "--home-root", home), FakeStdin(TOKEN), 0),
        (("connect", "cakemail", "--handle", "two", "--token-stdin", "--no-verify", "--home-root", home),
         FakeStdin(PAT), 0),
        (("connections", "list", "--home-root", home), None, 0),
        (("connections", "list", "--json", "--home-root", home), None, 0),
        (("installs", "list", "--home-root", home), None, 0),
        (("connections", "remove", "cakemail", "two", "--home-root", home), None, 0),
        (("connections", "remove", "cakemail", "missing", "--home-root", home), None, 1),
    ]
    for argv, stdin, expected in commands:
        assert run(monkeypatch, *argv, stdin=stdin) == expected, argv
        assert db.exists(), argv
        assert mode(db) == 0o600, argv
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.exists():
                assert mode(side) & 0o077 == 0, (argv, suffix)
        # A wider mode is tightened by the next command.
    os.chmod(db, 0o644)
    assert run(monkeypatch, "connections", "list", "--home-root", home) == 0
    assert mode(db) == 0o600
    capsys.readouterr()


# ------------------------------------------------------ run environments


_GUARD_DUMP = """
import hashlib, json, os, pathlib
home = pathlib.Path(os.environ["FABRI_HOME"])
sid = os.environ.get("FABRI_SESSION_ID", "sess")
trace = home / ".fabri" / "traces" / f"{sid}.jsonl"
trace.parent.mkdir(parents=True, exist_ok=True)
trace.write_text("")
body = {
    "names": sorted(os.environ),
    "values": sorted(os.environ.values()),
    "creds": {k: hashlib.sha256(v.encode()).hexdigest()
              for k, v in os.environ.items() if k.startswith("FABRI_CRED_")},
}
print(json.dumps({"session_id": sid, "success": True, "outcome": "success",
                  "final_text": json.dumps(body), **body}))
"""


def _guard_config(tmp_path):
    config = tmp_path / "run.yaml"
    config.write_text(yaml.safe_dump({"tools": {"sources": [{
        "name": "ck", "kind": "http", "url": "https://api.example.test/x", "method": "GET",
        "headers": {"Authorization": {"secret_ref": "cakemail:main", "prefix": "Bearer "}},
    }]}}))
    return config


def _guard_env(monkeypatch, db):
    store = ConnectionStore(db)
    store.upsert(provider="cakemail", handle="main", access_token=TOKEN)
    store.upsert(provider="cakemail", handle="other", access_token=PAT)
    store.upsert(provider="linear", handle="ws1", access_token=ACCESS)
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    monkeypatch.setenv("FABRI_CRED_SLACK_ACME", "slack-service-env-value")
    monkeypatch.delenv("FABRI_RUN_ENV_ALLOW", raising=False)
    monkeypatch.delenv("FABRI_CRED_AUDIT_LOG", raising=False)
    monkeypatch.delenv("FABRI_CRED_SNAPSHOT", raising=False)


def test_serve_run_gets_broker_env_and_no_values(tmp_path, monkeypatch):
    """A run under fabri serve receives the broker's socket and key for its
    credentialed source, and no token value and no install db path, even when
    the service holds many tokens."""
    from fabri.service.service import FabriService

    home_root = tmp_path / "serve"
    home_root.mkdir()
    db = home_root / "installs.db"
    _guard_env(monkeypatch, db)
    config = _guard_config(tmp_path)
    script = tmp_path / "dump.py"
    script.write_text(_GUARD_DUMP)

    svc = FabriService(template_config=str(config), home_root=home_root,
                       command_builder=lambda task, cfg, sid, home: [sys.executable, str(script)])
    try:
        seen = json.loads(svc.result(svc.submit("t"), timeout=30)["final_text"])
    finally:
        svc.close()

    assert sorted(seen["creds"]) == ["FABRI_CRED_BROKER_KEY", "FABRI_CRED_BROKER_SOCKET"]
    assert "FABRI_INSTALL_DB" not in seen["names"]
    db_path_seen = any(str(db) in v for v in seen["values"])
    assert not db_path_seen, "the install db path reached the run"
    for label, secret in (("TOKEN", TOKEN), ("PAT", PAT), ("ACCESS", ACCESS),
                          ("service env", "slack-service-env-value")):
        leaked = any(secret in v for v in seen["values"])
        assert not leaked, f"{label} value reached the run"


def test_serve_run_snapshot_gets_only_its_declared_refs(tmp_path, monkeypatch):
    """The FABRI_CRED_SNAPSHOT=1 path (credential_snapshot=True): the run
    receives FABRI_CRED_* for the refs its own sources declare and no
    FABRI_INSTALL_DB, even when the service holds many."""
    import hashlib

    db = tmp_path / "installs.db"
    _guard_env(monkeypatch, db)
    config = _guard_config(tmp_path)
    script = tmp_path / "dump.py"
    script.write_text(_GUARD_DUMP)

    handle = launch_run("t", config_path=config, fabri_home=tmp_path / "home", session_id="sess-guard",
                        command=[sys.executable, str(script)], credential_snapshot=True)
    seen = handle.result(timeout=30)

    assert sorted(seen["creds"]) == ["FABRI_CRED_CAKEMAIL_MAIN"]
    right_value = seen["creds"]["FABRI_CRED_CAKEMAIL_MAIN"] == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert right_value, "the run did not get its declared value"
    assert "FABRI_INSTALL_DB" not in seen["names"]
    db_path_seen = any(str(db) in v for v in seen["values"])
    assert not db_path_seen, "the install db path reached the run"
    for label, secret in (("PAT", PAT), ("ACCESS", ACCESS), ("service env", "slack-service-env-value")):
        leaked = any(secret in v for v in seen["values"])
        assert not leaked, f"{label} value reached the run"


def test_upstream_body_echoing_the_auth_header_is_redacted(monkeypatch):
    import httpx
    from fabri.tools.credential_store import EnvCredentialStore
    from fabri.tools.http_call import send
    secret = "ck_pat_" + "a" * 40
    monkeypatch.setenv("FABRI_CRED_CAKEMAIL_MAIN", secret)
    echo = httpx.MockTransport(lambda r: httpx.Response(401, json={"you_sent": r.headers.get("Authorization")}))
    out = send(httpx.Client(transport=echo), "GET", "https://api.example.test/x",
               headers={"Authorization": {"secret_ref": "cakemail:main", "prefix": "Bearer "}},
               store=EnvCredentialStore())
    assert secret not in repr(out)
