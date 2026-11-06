"""`fabri connect` over loopback OAuth: a browser sign-in whose one callback
lands on 127.0.0.1, a state check, a PKCE code exchange and a stored
connection. No real network: the vendor is an httpx MockTransport and the
browser is a function that calls the loopback port."""
from __future__ import annotations

import http.client
import io
import socket
import sys
import threading
import types
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from fabri.cli import main
from fabri.service.connection_store import STATE_SECRET_ENV, ConnectionStore
from fabri.tools import connections
from fabri.tools import connector_catalog as catalog
from fabri.tools.connections import ConnectError
from fabri.tools.connections.loopback import run_loopback_flow
from fabri.tools.connections.oauth import pkce_challenge
from fabri.tools.connector_catalog import Connector, OAuthSpec

pytestmark = pytest.mark.unit

CLIENT_ID = "client-id-loopback"
CLIENT_SECRET = "client-secret-loopback-unique"
CODE = "auth-code-loopback-unique"
ACCESS = "access-loopback-unique"
REFRESH = "refresh-loopback-unique"
TOKEN_URL = "https://oauth.example.test/token"
AUTHORIZE_URL = "https://accounts.example.test/auth"
USERINFO_URL = "https://id.example.test/userinfo"
READONLY = "https://scope.example.test/readonly"

_real_getaddrinfo = socket.getaddrinfo


class FakeStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Vendor hosts resolve to a public address (the MockTransport answers);
    the loopback address resolves for real so the fake browser can reach the
    callback server."""
    def getaddrinfo(host, port, *args, **kwargs):
        if host in ("127.0.0.1", "localhost", "::1"):
            return _real_getaddrinfo(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("FABRI_INSTALL_DB", raising=False)
    monkeypatch.delenv(STATE_SECRET_ENV, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _entry(name: str = "zmail", *, loopback: bool = True) -> Connector:
    spec = OAuthSpec(AUTHORIZE_URL, TOKEN_URL,
                     scopes_default=("openid", "email", "https://scope.example.test/send"),
                     scopes_optional={"readonly": READONLY, "compose": "https://scope.example.test/compose"},
                     client_id_env="ZMAIL_CLIENT_ID", secret_env="ZMAIL_CLIENT_SECRET", pkce=True,
                     loopback=loopback, extra_authorize={"access_type": "offline", "prompt": "consent"},
                     identity_url=USERINFO_URL)
    return Connector(name, "Zmail", "A loopback OAuth test connector.", ("http",), "ZMAIL_TOKEN", name,
                     "https://api.example.test", auth_kind="oauth", scope_default="user", oauth=spec)


def _identity(tok, get_json):
    info = get_json(USERINFO_URL, {"Authorization": "Bearer " + tok["access_token"]})
    return None, info.get("email")


@pytest.fixture
def zmail(monkeypatch):
    """Register the test connector and a fake vendor on the connect transport hook."""
    entry = _entry()
    module = types.ModuleType("zmail_test_connector")
    module.ENTRY = entry
    module.identity = _identity
    monkeypatch.setitem(catalog._MODULES, entry.name, module)
    monkeypatch.setitem(catalog.CONNECTORS, entry.name, entry)
    monkeypatch.setenv("ZMAIL_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("ZMAIL_CLIENT_SECRET", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url == TOKEN_URL and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("code") != CODE:
                return httpx.Response(400, json={"error": "invalid_grant", "echo": form.get("code")})
            return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH,
                                             "expires_in": 3599, "token_type": "Bearer",
                                             "scope": "openid email https://scope.example.test/send"})
        if url == USERINFO_URL:
            if request.headers.get("authorization") != f"Bearer {ACCESS}":
                return httpx.Response(401)
            return httpx.Response(200, json={"email": "Ada.Lovelace@example.test"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(connections, "_transport", transport)
    return types.SimpleNamespace(entry=entry, seen=seen, transport=transport)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _get(redirect_uri: str, params: dict[str, str], *, host_header: str | None = None,
         path: str | None = None) -> tuple[int, str]:
    parts = urlsplit(redirect_uri)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        headers = {"Host": host_header} if host_header else {}
        conn.request("GET", (path or parts.path) + "?" + urlencode(params), headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


class FakeBrowser:
    """Plays the user: follows the authorize URL to the callback with a code
    and the issued state, after poking the port the way a stray local process
    would."""

    def __init__(self, *, code: str = CODE, probes: bool = True, error: str | None = None,
                 repeat: bool = True) -> None:
        self.code = code
        self.repeat = repeat
        self.probes = probes
        self.error = error
        self.urls: list[str] = []
        self.answers: list[tuple[int, str]] = []

    def __call__(self, url: str) -> None:
        self.urls.append(url)
        query = _query(url)
        redirect_uri = query["redirect_uri"]
        if self.probes:
            self.answers.append(_get(redirect_uri, {"code": "stray", "state": "forged"}))
            self.answers.append(_get(redirect_uri, {"code": "stray", "state": query["state"]},
                                     host_header="evil.example.test"))
            self.answers.append(_get(redirect_uri, {"state": query["state"]}, path="/favicon.ico"))
        params = {"state": query["state"]}
        if self.error:
            params["error"] = self.error
        else:
            params["code"] = self.code
        self.answers.append(_get(redirect_uri, params))
        if self.repeat:
            # A second callback after the first is turned away.
            self.answers.append(_get(redirect_uri, params))


def _store(tmp_path) -> ConnectionStore:
    return ConnectionStore(tmp_path / "installs.db")


def _assert_clean(text: str) -> None:
    for secret in (ACCESS, REFRESH, CODE, CLIENT_SECRET):
        assert secret not in text


def test_loopback_round_trip_with_fake_browser(zmail, tmp_path, caplog):
    store = _store(tmp_path)
    browser = FakeBrowser()
    row = run_loopback_flow(store, zmail.entry, handle="main", scope_kind="org", scopes=["readonly"],
                            open_url=browser, timeout_s=10)
    assert row["provider"] == "zmail" and row["handle"] == "main"
    assert row["display_name"] == "Ada.Lovelace@example.test"
    assert row["meta"]["source"] == "oauth"
    assert store.get_token("zmail", "main") == ACCESS

    # The authorize URL: loopback redirect, PKCE, offline access, the asked-for scope.
    query = _query(browser.urls[0])
    redirect = urlsplit(query["redirect_uri"])
    assert redirect.scheme == "http" and redirect.hostname == "127.0.0.1" and redirect.path == "/callback"
    assert query["client_id"] == CLIENT_ID and query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert READONLY in query["scope"].split(" ")

    # The stray, wrong-host and favicon requests were refused; the real one
    # got the close-the-tab page; a second callback was turned away.
    statuses = [status for status, _ in browser.answers]
    assert statuses == [400, 400, 404, 200, 410]
    assert "close this tab" in browser.answers[3][1]

    # The token exchange carried the code, the same redirect URI and a
    # verifier that matches the challenge; no client secret without the env.
    token_requests = [r for r in zmail.seen if str(r.url) == TOKEN_URL]
    assert len(token_requests) == 1
    form = {k: v[0] for k, v in parse_qs(token_requests[0].content.decode()).items()}
    assert form["code"] == CODE and form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == query["redirect_uri"]
    assert pkce_challenge(form["code_verifier"]) == query["code_challenge"]
    assert "client_secret" not in form
    _assert_clean(caplog.text)

    # The server is gone once the flow returns.
    with pytest.raises(OSError):
        _get(query["redirect_uri"], {"state": "x"})


def test_loopback_user_scope_stores_under_the_given_user_id(zmail, tmp_path):
    store = _store(tmp_path)
    row = run_loopback_flow(store, zmail.entry, handle="user-42", scope_kind="user", scopes=None,
                            open_url=FakeBrowser(probes=False), timeout_s=10)
    assert row["scope_kind"] == "user" and row["handle"] == "user-42"


def test_loopback_timeout(zmail, tmp_path):
    store = _store(tmp_path)
    opened: list[str] = []
    with pytest.raises(ConnectError, match=r"no callback within 0.3 s"):
        run_loopback_flow(store, zmail.entry, handle="main", scope_kind="org", scopes=None,
                          open_url=opened.append, timeout_s=0.3)
    assert opened and store.list("zmail") == []
    with pytest.raises(OSError):
        _get(_query(opened[0])["redirect_uri"], {"state": "x"})


def test_loopback_vendor_error_stores_nothing(zmail, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ConnectError, match=r"sign-in was not completed \(access_denied\)"):
        run_loopback_flow(store, zmail.entry, handle="main", scope_kind="org", scopes=None,
                          open_url=FakeBrowser(probes=False, error="access_denied"), timeout_s=10)
    assert store.list("zmail") == []
    assert not [r for r in zmail.seen if str(r.url) == TOKEN_URL]


def test_loopback_refuses_non_loopback_host_and_entry(zmail, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ConnectError, match="loopback"):
        run_loopback_flow(store, zmail.entry, handle="main", scope_kind="org", scopes=None,
                          open_url=lambda url: None, host="0.0.0.0")
    with pytest.raises(ConnectError, match="/connect/slack"):
        run_loopback_flow(store, catalog.CONNECTORS["slack"], handle="main", scope_kind="org", scopes=None,
                          open_url=lambda url: None)


# -- the CLI ------------------------------------------------------------------


def run(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    monkeypatch.setattr(sys, "stdin", FakeStdin(""))
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser(probes=False)
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: fake(url) or True)
    return fake


def test_cli_connect_opens_browser_and_prints_next_step(zmail, browser, tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("ZMAIL_CLIENT_SECRET", CLIENT_SECRET)
    assert run(monkeypatch, "connect", "zmail", "--home-root", str(tmp_path), "--timeout-s", "10") == 0
    out, err = capsys.readouterr()
    assert "connected zmail:main (Ada.Lovelace@example.test)" in out
    assert "fabri setups tools add connector zmail --workspace main --scope org:<org>" in out
    assert len(browser.urls) == 1 and browser.urls[0] in err
    _assert_clean(out + err + caplog.text)
    assert _store(tmp_path).get_token("zmail", "main") == ACCESS
    token_request = [r for r in zmail.seen if str(r.url) == TOKEN_URL][0]
    assert "client_secret=" in token_request.content.decode()


def test_cli_connect_user_scope_prints_actor_line(zmail, browser, tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, "connect", "zmail", "--scope", "user", "--handle", "u-7",
               "--home-root", str(tmp_path), "--timeout-s", "10") == 0
    out = capsys.readouterr().out
    assert "connected zmail:u-7" in out
    assert "--workspace '${actor}'" in out
    assert _store(tmp_path).get("zmail", "u-7")["scope_kind"] == "user"


def test_connect_refuses_oauth_for_non_loopback_entry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: pytest.fail("no browser for slack"))
    for argv in (("--no-browser",), ("--scope", "user"), ("--scopes", "x")):
        assert run(monkeypatch, "connect", "slack", *argv, "--home-root", str(tmp_path)) == 2
        err = capsys.readouterr().err
        assert "GET /connect/slack" in err
    assert not (tmp_path / "installs.db").exists()


def test_scopes_flag_maps_short_names_and_rejects_unknown(zmail, browser, tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, "connect", "zmail", "--scopes", "nope", "--home-root", str(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "unknown scope name 'nope'" in err and "compose, readonly" in err
    assert not browser.urls
    # A full scope string is not a short name either.
    assert run(monkeypatch, "connect", "zmail", "--scopes", READONLY, "--home-root", str(tmp_path)) == 2
    capsys.readouterr()

    assert run(monkeypatch, "connect", "zmail", "--scopes", "readonly", "compose",
               "--home-root", str(tmp_path), "--timeout-s", "10") == 0
    scopes = _query(browser.urls[0])["scope"].split(" ")
    assert READONLY in scopes and "https://scope.example.test/compose" in scopes
    assert scopes[:3] == ["openid", "email", "https://scope.example.test/send"]


def test_no_browser_prints_url_only(zmail, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: pytest.fail("--no-browser opened a browser"))
    printed: list[str] = []
    real_print = print

    # The user copies the printed URL into a browser: follow it from a thread
    # once it shows up on stderr.
    def watching_print(*args, **kwargs):
        real_print(*args, **kwargs)
        text = " ".join(str(a) for a in args)
        if kwargs.get("file") is sys.stderr and text.startswith(AUTHORIZE_URL):
            printed.append(text)
            threading.Thread(target=FakeBrowser(probes=False, repeat=False), args=(text,), daemon=True).start()

    monkeypatch.setattr("builtins.print", watching_print)
    assert run(monkeypatch, "connect", "zmail", "--no-browser", "--home-root", str(tmp_path),
               "--timeout-s", "10") == 0
    out, err = capsys.readouterr()
    assert len(printed) == 1 and printed[0] in err
    assert "opening the browser" not in err
    assert "connected zmail:main" in out
    _assert_clean(out + err)


def test_cli_oauth_refuses_token_flags_and_unconfigured(zmail, tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, "connect", "zmail", "--token-stdin", "--home-root", str(tmp_path)) == 2
    assert "--token-stdin is for pasted tokens" in capsys.readouterr().err
    monkeypatch.delenv("ZMAIL_CLIENT_ID")
    assert run(monkeypatch, "connect", "zmail", "--home-root", str(tmp_path)) == 2
    assert "set ZMAIL_CLIENT_ID" in capsys.readouterr().err
    assert not (tmp_path / "installs.db").exists()


def test_cli_oauth_timeout_exits_1(zmail, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    assert run(monkeypatch, "connect", "zmail", "--home-root", str(tmp_path), "--timeout-s", "0.3") == 1
    assert "connect failed: no callback within 0.3 s" in capsys.readouterr().err
