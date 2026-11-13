"""`fabri connect` and `fabri connections` store, list and remove connections
offline, verify tokens first, and never print or log a credential."""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import stat
import sys

import httpx
import pytest

from fabri.cli import main
from fabri.service.connection_store import ConnectionStore
from fabri.tools.connections.cakemail_login import CAKEMAIL_PATS_URL, CAKEMAIL_TOKEN_URL, pat_scopes

pytestmark = pytest.mark.unit

TOKEN = "ck_pat_" + "f0" * 20
PAT = "ck_pat_" + "9e" * 20
PASSWORD = "pw-Correct-Horse-unique"
ACCESS = "jwt-login-unique"
VERIFY_URL = "https://api.cakemail.dev/users/self"
NEXT_STEP = "fabri setups tools add connector cakemail --workspace {h} --scope org:<org>"


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("FABRI_INSTALL_DB", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                      ("93.184.216.34", 0))])


@pytest.fixture
def cakemail(monkeypatch):
    """A fake Cakemail API on the connect transport hook. `state` controls it
    and records every request."""
    state = {"seen": [], "verify_status": 200, "valid": {TOKEN, PAT}}

    def handler(request: httpx.Request) -> httpx.Response:
        state["seen"].append(request)
        url = str(request.url)
        if url == VERIFY_URL and request.method == "GET":
            auth = request.headers.get("authorization", "")
            if state["verify_status"] != 200:
                return httpx.Response(state["verify_status"], json={"detail": f"echo {auth}"})
            if auth.removeprefix("Bearer ") not in state["valid"]:
                return httpx.Response(401, json={"detail": f"echo {auth}"})
            return httpx.Response(200, json={"data": {"id": 1, "email": "ops@example.com"}})
        if url == CAKEMAIL_TOKEN_URL:
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600, "accounts": [7]})
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


def store(home) -> ConnectionStore:
    return ConnectionStore(home / "installs.db")


def assert_clean(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text


def test_connect_token_stdin_verifies_and_stores(tmp_path, monkeypatch, capsys, caplog, cakemail):
    caplog.set_level(logging.DEBUG)
    code = run(monkeypatch, "connect", "cakemail", "--token-stdin", "--home-root", str(tmp_path),
               stdin=FakeStdin(TOKEN + "\nignored second line\n"))
    assert code == 0
    [verify] = cakemail["seen"]
    assert verify.method == "GET" and str(verify.url) == VERIFY_URL
    assert verify.headers["authorization"] == f"Bearer {TOKEN}"
    out, err = capsys.readouterr()
    assert "connected cakemail:main" in out
    assert NEXT_STEP.format(h="main") in out
    assert_clean(out + err + caplog.text, TOKEN)
    s = store(tmp_path)
    assert s.get_token("cakemail", "main") == TOKEN
    row = s.get("cakemail", "main")
    assert row["scope_kind"] == "org" and row["display_name"] == "ops@example.com"
    assert row["meta"] == {"source": "static"}
    assert stat.S_IMODE(os.stat(tmp_path / "installs.db").st_mode) == 0o600


def test_connect_existing_row_says_replaced(tmp_path, monkeypatch, capsys, cakemail):
    assert run(monkeypatch, "connect", "cakemail", "--handle", "acct1", "--token-stdin", "--home-root",
               str(tmp_path), stdin=FakeStdin(TOKEN)) == 0
    capsys.readouterr()
    assert run(monkeypatch, "connect", "cakemail", "--handle", "acct1", "--token-stdin", "--home-root",
               str(tmp_path), stdin=FakeStdin(PAT)) == 0
    out = capsys.readouterr().out
    assert "replaced cakemail:acct1" in out and NEXT_STEP.format(h="acct1") in out
    assert store(tmp_path).get_token("cakemail", "acct1") == PAT


def test_connect_no_verify_skips_the_request(tmp_path, monkeypatch, capsys, cakemail):
    assert run(monkeypatch, "connect", "cakemail", "--token-stdin", "--no-verify", "--home-root", str(tmp_path),
               stdin=FakeStdin("ck_pat_unchecked")) == 0
    assert cakemail["seen"] == []
    assert store(tmp_path).get_token("cakemail", "main") == "ck_pat_unchecked"


def test_connect_refuses_without_tty_and_flag(tmp_path, monkeypatch, capsys, cakemail):
    code = run(monkeypatch, "connect", "cakemail", "--home-root", str(tmp_path), stdin=FakeStdin(TOKEN))
    assert code == 2
    assert "stdin is not a terminal; pass --token-stdin" in capsys.readouterr().err
    assert cakemail["seen"] == []
    assert not (tmp_path / "installs.db").exists()


@pytest.mark.parametrize("handle", ["-lead", "a b", "x" * 81, "acme/1", "default"])
def test_connect_bad_handle(tmp_path, monkeypatch, capsys, cakemail, handle):
    code = run(monkeypatch, "connect", "cakemail", f"--handle={handle}", "--token-stdin", "--home-root",
               str(tmp_path), stdin=FakeStdin(TOKEN))
    assert code == 2
    err = capsys.readouterr().err
    assert "bad --handle" in err
    if handle != "default":
        assert "letters, digits" in err
    assert cakemail["seen"] == [] and not (tmp_path / "installs.db").exists()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_connect_verify_failure_stores_nothing(tmp_path, monkeypatch, capsys, caplog, cakemail, status):
    caplog.set_level(logging.DEBUG)
    cakemail["verify_status"] = status
    code = run(monkeypatch, "connect", "cakemail", "--token-stdin", "--home-root", str(tmp_path),
               stdin=FakeStdin(TOKEN))
    assert code == 1
    out, err = capsys.readouterr()
    expected = "cakemail refused the token" if status in (401, 403) else f"cakemail verify failed (HTTP {status})"
    assert expected in err
    assert_clean(out + err + caplog.text, TOKEN, "echo")
    assert store(tmp_path).list() == []


def test_connect_prompts_on_tty(tmp_path, monkeypatch, capsys, cakemail):
    prompts = []

    def fake_getpass(prompt="Password: "):
        prompts.append(prompt)
        return TOKEN + "  "

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert run(monkeypatch, "connect", "cakemail", "--home-root", str(tmp_path),
               stdin=FakeStdin("", tty=True)) == 0
    assert prompts == ["token: "]
    out, err = capsys.readouterr()
    assert "connected cakemail:main" in out
    assert_clean(out + err, TOKEN)
    assert store(tmp_path).get_token("cakemail", "main") == TOKEN


def test_connect_interrupted_prompt_exits_130_without_a_row(tmp_path, monkeypatch, capsys, cakemail):
    def interrupted(prompt="Password: "):
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", interrupted)
    assert run(monkeypatch, "connect", "cakemail", "--home-root", str(tmp_path),
               stdin=FakeStdin("", tty=True)) == 130
    assert not (tmp_path / "installs.db").exists() or store(tmp_path).list() == []


def test_connections_list_and_json_have_no_tokens(tmp_path, monkeypatch, capsys, cakemail):
    for handle, token in (("main", TOKEN), ("acct2", PAT)):
        assert run(monkeypatch, "connect", "cakemail", "--handle", handle, "--token-stdin", "--home-root",
                   str(tmp_path), stdin=FakeStdin(token)) == 0
    capsys.readouterr()
    assert run(monkeypatch, "connections", "list", "--home-root", str(tmp_path)) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0].split() == ["PROVIDER", "HANDLE", "SCOPE", "NAME", "STATUS", "EXPIRES"]
    assert any(line.split()[:3] == ["cakemail", "main", "org"] for line in lines[1:])
    assert any(line.split()[:3] == ["cakemail", "acct2", "org"] for line in lines[1:])
    assert_clean(out + err, TOKEN, PAT)
    assert run(monkeypatch, "connections", "list", "--json", "--provider", "cakemail",
               "--home-root", str(tmp_path)) == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert sorted(r["handle"] for r in rows) == ["acct2", "main"]
    assert all("token" not in key for r in rows for key in r)
    assert_clean(out, TOKEN, PAT)
    assert run(monkeypatch, "connections", "list", "--provider", "slack", "--home-root", str(tmp_path)) == 0
    assert capsys.readouterr().out.strip() == "no connections"


def test_connections_list_warns_when_file_is_too_open(tmp_path, monkeypatch, capsys, cakemail):
    assert run(monkeypatch, "connect", "cakemail", "--token-stdin", "--home-root", str(tmp_path),
               stdin=FakeStdin(TOKEN)) == 0
    capsys.readouterr()
    os.chmod(tmp_path / "installs.db", 0o644)
    assert run(monkeypatch, "connections", "list", "--home-root", str(tmp_path)) == 0
    assert "was readable by other users; it is now 0600" in capsys.readouterr().err
    assert stat.S_IMODE(os.stat(tmp_path / "installs.db").st_mode) == 0o600


def test_connections_list_needs_the_database(tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, "connections", "list", "--home-root", str(tmp_path / "nope")) == 1
    assert "no install database" in capsys.readouterr().err


def test_connections_remove(tmp_path, monkeypatch, capsys, cakemail):
    assert run(monkeypatch, "connect", "cakemail", "--handle", "acct1", "--token-stdin", "--home-root",
               str(tmp_path), stdin=FakeStdin(TOKEN)) == 0
    capsys.readouterr()
    assert run(monkeypatch, "connections", "remove", "cakemail", "acct1", "--home-root", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "removed cakemail:acct1" in out and "until someone revokes it there" in out
    assert store(tmp_path).get_token("cakemail", "acct1") is None
    assert run(monkeypatch, "connections", "remove", "cakemail", "acct1", "--home-root", str(tmp_path)) == 1
    assert "no connection cakemail:acct1" in capsys.readouterr().err


def test_connect_user_scope_refused_in_this_release(tmp_path, monkeypatch, capsys, cakemail):
    code = run(monkeypatch, "connect", "cakemail", "--scope", "user", "--token-stdin", "--home-root",
               str(tmp_path), stdin=FakeStdin(TOKEN))
    assert code == 2
    assert "GET /connect/cakemail" in capsys.readouterr().err
    assert cakemail["seen"] == [] and not (tmp_path / "installs.db").exists()


def test_connect_unknown_provider(tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, "connect", "nope", "--token-stdin", "--home-root", str(tmp_path),
               stdin=FakeStdin(TOKEN)) == 2
    assert "unknown connector 'nope'" in capsys.readouterr().err


def test_connect_login_creates_pat_and_discards_password(tmp_path, monkeypatch, capsys, caplog, cakemail):
    caplog.set_level(logging.DEBUG)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "ops@example.com")
    monkeypatch.setattr("getpass.getpass", lambda prompt="Password: ": prompts.append(prompt) or PASSWORD)
    code = run(monkeypatch, "connect", "cakemail", "--login", "--handle", "acct1", "--account-id", "7",
               "--expires-days", "30", "--home-root", str(tmp_path), stdin=FakeStdin("", tty=True))
    assert code == 0
    assert prompts == ["Cakemail username: ", "Cakemail password: "]
    login, create, verify = cakemail["seen"]
    assert str(login.url) == CAKEMAIL_TOKEN_URL and str(create.url) == CAKEMAIL_PATS_URL
    assert create.headers["authorization"] == f"Bearer {ACCESS}"
    body = json.loads(create.content)
    assert body["scopes"] == list(pat_scopes()) and body["allowed_account_ids"] == [7]
    assert body["name"].startswith("fabri ")
    assert verify.headers["authorization"] == f"Bearer {PAT}"
    out, err = capsys.readouterr()
    assert "connected cakemail:acct1" in out and NEXT_STEP.format(h="acct1") in out
    assert_clean(out + err + caplog.text, PASSWORD, ACCESS, PAT)
    s = store(tmp_path)
    assert s.get_token("cakemail", "acct1") == PAT
    assert_clean(json.dumps(s.list()), PASSWORD, ACCESS, PAT)
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("installs.db"))
    assert PASSWORD.encode() not in raw and ACCESS.encode() not in raw


def test_connect_login_without_tty_exits_before_asking(tmp_path, monkeypatch, capsys, cakemail):
    def no_prompt(*a, **k):
        raise AssertionError("prompted")

    monkeypatch.setattr("builtins.input", no_prompt)
    monkeypatch.setattr("getpass.getpass", no_prompt)
    assert run(monkeypatch, "connect", "cakemail", "--login", "--home-root", str(tmp_path),
               stdin=FakeStdin("")) == 2
    assert "--login needs a terminal" in capsys.readouterr().err
    assert cakemail["seen"] == []


def test_connect_login_is_for_cakemail_only(tmp_path, monkeypatch, capsys, cakemail):
    assert run(monkeypatch, "connect", "linear", "--login", "--home-root", str(tmp_path),
               stdin=FakeStdin("", tty=True)) == 2
    assert "--login is for cakemail" in capsys.readouterr().err


def test_login_options_need_login(tmp_path, monkeypatch, capsys, cakemail):
    assert run(monkeypatch, "connect", "cakemail", "--token-stdin", "--account-id", "7", "--home-root",
               str(tmp_path), stdin=FakeStdin(TOKEN)) == 2
    assert "go with --login" in capsys.readouterr().err
