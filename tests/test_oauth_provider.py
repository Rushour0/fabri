"""The generic OAuth code provider, the signed state and the catalog OAuth fields."""
from __future__ import annotations

import socket
import sqlite3
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fabri.service import connection_store as cs_module
from fabri.service import linear_oauth, oauth_state
from fabri.service.connection_store import PENDING_TTL_S, STATE_SECRET_ENV, ConnectionStore
from fabri.tools.connections import ConnectError
from fabri.tools.connections.oauth import OAuthCodeProvider, pkce_challenge
from fabri.tools.connector_catalog import (
    CONNECTORS,
    Connector,
    ConnectorError,
    OAuthSpec,
    list_connectors,
)
from fabri.tools.connector_catalog import slack as slack_module

pytestmark = pytest.mark.unit

T0 = 2_000_000.0
REDIRECT = "https://fabri.example.test/connect/{p}/callback"
CODE = "auth-code-xyz"
ACCESS = "access-value-1"
REFRESH = "refresh-value-1"
CLIENT_SECRET = "client-secret-value"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.delenv(STATE_SECRET_ENV, raising=False)


@pytest.fixture
def store(tmp_path):
    return ConnectionStore(tmp_path / "installs.db")


def _pkce_entry() -> Connector:
    """Slack's module (and so its identity hook) with a PKCE spec, the shape
    a loopback provider like Gmail has."""
    spec = OAuthSpec("https://auth.example.test/authorize", "https://auth.example.test/token",
                     scopes_default=("a", "b"), scopes_optional={"extra": "https://scope.example.test/extra"},
                     client_id_env="ZZ_CLIENT_ID", secret_env="ZZ_CLIENT_SECRET", pkce=True,
                     extra_authorize={"access_type": "offline"})
    return replace(CONNECTORS["slack"], oauth=spec)


class Recorder:
    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _slack_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "access_token": ACCESS, "refresh_token": REFRESH,
                                     "expires_in": 3600, "scope": "a,b",
                                     "team": {"id": "T123", "name": "Acme"}})


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _pending_rows(store) -> list[sqlite3.Row]:
    with sqlite3.connect(store.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM pending_oauth").fetchall()


def _no_secret_text(text: str) -> None:
    for value in (CODE, ACCESS, REFRESH, CLIENT_SECRET):
        assert value not in text


# -- state ------------------------------------------------------------------


def test_state_round_trip_tamper_and_expiry():
    state = oauth_state.sign_state("k1", {"n": "abc", "p": "slack"}, ttl_s=60, now=T0)
    assert oauth_state.verify_state(state, "k1", now=T0 + 30) == {"n": "abc", "p": "slack"}
    assert oauth_state.verify_state(state, "k2", now=T0) is None
    assert oauth_state.verify_state(state, "k1", now=T0 + 61) is None
    body, expiry, sig = state.split(".")
    assert oauth_state.verify_state(f"{body}.{int(expiry) + 999}.{sig}", "k1", now=T0) is None
    assert oauth_state.verify_state("garbage", "k1", now=T0) is None
    assert oauth_state.verify_state(state, "", now=T0) is None
    with pytest.raises(ValueError):
        oauth_state.sign_state("", {"n": "x"})


# -- begin ------------------------------------------------------------------


def test_begin_writes_pending_row_and_pkce_challenge(store, monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    provider = OAuthCodeProvider(_pkce_entry())
    url = provider.begin(store, redirect_uri=REDIRECT.format(p="slack"), scope_kind="org", handle="main",
                         user_id=None, scopes=["extra"], now=T0)
    assert url.startswith("https://auth.example.test/authorize?")
    query = _query(url)
    assert query["client_id"] == "cid" and query["response_type"] == "code"
    assert query["redirect_uri"] == REDIRECT.format(p="slack")
    assert query["scope"] == "a b https://scope.example.test/extra"
    assert query["code_challenge_method"] == "S256" and query["access_type"] == "offline"
    rows = _pending_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert pkce_challenge(row["code_verifier"]) == query["code_challenge"]
    assert row["code_verifier"] not in url
    assert row["provider"] == "slack" and row["handle"] == "main" and row["scope_kind"] == "org"
    assert row["expires_at"] == T0 + PENDING_TTL_S
    payload = oauth_state.verify_state(query["state"], store.state_secret(), now=T0)
    assert payload == {"n": row["nonce"], "p": "slack"}
    with pytest.raises(ConnectError, match="no scope named"):
        provider.begin(store, redirect_uri="https://x.test/cb", scope_kind="org", handle=None,
                       user_id=None, scopes=["nope"])
    with pytest.raises(ConnectError, match="signed-in user"):
        provider.begin(store, redirect_uri="https://x.test/cb", scope_kind="user", handle=None, user_id=None)


def test_begin_without_client_id_is_unconfigured(store, monkeypatch):
    monkeypatch.delenv("ZZ_CLIENT_ID", raising=False)
    provider = OAuthCodeProvider(_pkce_entry())
    assert provider.configured() is False
    with pytest.raises(ConnectError, match="not configured"):
        provider.begin(store, redirect_uri="https://x.test/cb", scope_kind="org", handle=None, user_id=None)
    assert _pending_rows(store) == []
    # Without PKCE the client secret is needed too.
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)
    assert OAuthCodeProvider(CONNECTORS["slack"]).configured() is False
    monkeypatch.setenv("SLACK_CLIENT_SECRET", CLIENT_SECRET)
    assert OAuthCodeProvider(CONNECTORS["slack"]).configured() is True
    with pytest.raises(ConnectError, match="connects with a token"):
        OAuthCodeProvider(CONNECTORS["github"])


# -- finish -----------------------------------------------------------------


def _begin(store, provider, *, scope_kind="org", handle=None, user_id=None, now=T0):
    url = provider.begin(store, redirect_uri=REDIRECT.format(p=provider.entry.name), scope_kind=scope_kind,
                         handle=handle, user_id=user_id, now=now)
    return _query(url)["state"]


def test_finish_rejects_bad_state_expired_state_and_reused_nonce(store, monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    rec = Recorder(_slack_ok)
    provider = OAuthCodeProvider(_pkce_entry(), transport=rec.transport())
    state = _begin(store, provider)
    with pytest.raises(ConnectError, match="state invalid"):
        provider.finish(store, state=state + "x", code=CODE, now=T0)
    with pytest.raises(ConnectError, match="state invalid"):
        provider.finish(store, state=state, code=CODE, now=T0 + PENDING_TTL_S + 1)
    # An empty code fails before any request and leaves the pending row.
    with pytest.raises(ConnectError, match="no authorization code"):
        provider.finish(store, state=state, code="", now=T0)
    assert rec.requests == [] and len(_pending_rows(store)) == 1
    provider.finish(store, state=state, code=CODE, now=T0 + 1)
    assert len(rec.requests) == 1
    with pytest.raises(ConnectError, match="no pending connection"):
        provider.finish(store, state=state, code=CODE, now=T0 + 2)
    assert len(rec.requests) == 1
    # A pending row past its own expiry is deleted and refused, even with a
    # state that is still in date.
    state2 = oauth_state.sign_state(store.state_secret(), {"n": "late", "p": "slack"}, now=T0 + 900)
    store.pending_put(nonce="late", provider="slack", handle=None, scope_kind="org", user_id=None,
                      code_verifier="v", scopes="a", redirect_uri="https://x.test/cb", now=T0)
    with pytest.raises(ConnectError, match="no pending connection"):
        provider.finish(store, state=state2, code=CODE, now=T0 + PENDING_TTL_S + 5)
    assert _pending_rows(store) == []
    # A state for another provider is refused.
    other = oauth_state.sign_state(store.state_secret(), {"n": "n1", "p": "linear"}, now=T0)
    with pytest.raises(ConnectError, match="another provider"):
        provider.finish(store, state=other, code=CODE, now=T0)


def test_finish_exchanges_with_verifier_and_stores_tokens(store, monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    monkeypatch.delenv("ZZ_CLIENT_SECRET", raising=False)
    rec = Recorder(_slack_ok)
    provider = OAuthCodeProvider(_pkce_entry(), transport=rec.transport())
    state = _begin(store, provider, handle="main")
    verifier = _pending_rows(store)[0]["code_verifier"]
    result = provider.finish(store, state=state, code=CODE, now=T0 + 5)
    (request,) = rec.requests
    assert str(request.url) == "https://auth.example.test/token" and request.method == "POST"
    form = _form(request)
    assert form == {"grant_type": "authorization_code", "code": CODE, "client_id": "cid",
                    "redirect_uri": REDIRECT.format(p="slack"), "code_verifier": verifier}
    assert "client_secret" not in form
    # The identity hook names the handle (Slack: the team id).
    assert result["provider"] == "slack" and result["handle"] == "T123"
    assert result["display_name"] == "Acme" and result["scope_kind"] == "org"
    assert result["expires_at"] == T0 + 5 + 3600
    assert result["meta"] == {"source": "oauth", "redirect_uri": REDIRECT.format(p="slack")}
    assert ACCESS not in repr(result) and REFRESH not in repr(result)
    assert not any("token" in key for key in result)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT access_token, refresh_token FROM connections WHERE handle='T123'"
                         ).fetchone() == (ACCESS, REFRESH)
    assert store.get_token("slack", "T123", now=T0 + 10) == ACCESS
    # With the secret set it goes in the form.
    monkeypatch.setenv("ZZ_CLIENT_SECRET", CLIENT_SECRET)
    state = _begin(store, provider)
    provider.finish(store, state=state, code=CODE, now=T0 + 6)
    assert _form(rec.requests[-1])["client_secret"] == CLIENT_SECRET


def test_finish_refuses_redirects_and_errors_without_leaking(store, monkeypatch, caplog):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    monkeypatch.setenv("ZZ_CLIENT_SECRET", CLIENT_SECRET)
    responses = iter([
        httpx.Response(302, headers={"Location": "https://evil.example.test/steal"}),
        httpx.Response(400, json={"error": "invalid_grant", "echo": CODE}),
        httpx.Response(200, json={"ok": False, "error": "bad_redirect_uri", "echo": CLIENT_SECRET}),
        httpx.Response(200, json={"ok": True, "access_token": ACCESS}),  # no team: identity raises
    ])
    rec = Recorder(lambda request: next(responses))
    provider = OAuthCodeProvider(_pkce_entry(), transport=rec.transport())
    messages = []
    for expected in ("HTTP 302", "HTTP 400", r"refused the code exchange \(bad_redirect_uri\)", "no team id"):
        state = _begin(store, provider)
        with pytest.raises(ConnectError, match=expected) as info:
            provider.finish(store, state=state, code=CODE, now=T0 + 1)
        messages.append(str(info.value))
    assert len(rec.requests) == 4
    assert all(str(r.url).startswith("https://auth.example.test/") for r in rec.requests)
    assert store.list() == []
    for text in [*messages, caplog.text]:
        _no_secret_text(text)


def test_finish_user_scope_forces_handle_to_user_id(store, monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    provider = OAuthCodeProvider(_pkce_entry(), transport=Recorder(_slack_ok).transport())
    state = _begin(store, provider, scope_kind="user", handle="ignored", user_id="u0a1b2c3")
    result = provider.finish(store, state=state, code=CODE, now=T0)
    assert result["handle"] == "u0a1b2c3" and result["scope_kind"] == "user"
    assert result["display_name"] == "Acme"
    assert store.scope_kind("slack", "u0a1b2c3") == "user"
    assert store.get("slack", "T123") is None


def test_finish_refuses_to_cross_an_existing_scope(store, monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    provider = OAuthCodeProvider(_pkce_entry(), transport=Recorder(_slack_ok).transport())

    # A user flow cannot replace an org row whose handle is the user's id.
    store.upsert(provider="slack", handle="u0a1b2c3", access_token="org-token", now=T0)
    state = _begin(store, provider, scope_kind="user", handle=None, user_id="u0a1b2c3")
    with pytest.raises(ConnectError, match="exists with another scope") as caught:
        provider.finish(store, state=state, code=CODE, now=T0)
    _no_secret_text(str(caught.value))
    assert store.get_token("slack", "u0a1b2c3", now=T0) == "org-token"
    assert store.scope_kind("slack", "u0a1b2c3") == "org"

    # An org flow cannot replace a user row that has the handle the vendor named.
    store.upsert(provider="slack", handle="T123", access_token="user-token", scope_kind="user", now=T0)
    state = _begin(store, provider, scope_kind="org", handle=None, user_id=None)
    with pytest.raises(ConnectError, match="exists with another scope"):
        provider.finish(store, state=state, code=CODE, now=T0)
    assert store.get_token("slack", "T123", now=T0) == "user-token"
    assert store.scope_kind("slack", "T123") == "user"

    # The same scope still replaces the row.
    store.delete("slack", "T123")
    store.upsert(provider="slack", handle="T123", access_token="old-org", now=T0)
    state = _begin(store, provider, scope_kind="org", handle=None, user_id=None)
    assert provider.finish(store, state=state, code=CODE, now=T0)["scope_kind"] == "org"
    assert store.get_token("slack", "T123", now=T0) == ACCESS


def test_slack_identity_uses_team_id_and_rejects_not_ok(store, monkeypatch, caplog):
    assert slack_module.identity({"ok": True, "team": {"id": "T9", "name": "Nine"}}, None) == ("T9", "Nine")
    with pytest.raises(ConnectorError, match=r"\(invalid_code\)") as info:
        slack_module.identity({"ok": False, "error": "invalid_code", "access_token": ACCESS}, None)
    _no_secret_text(str(info.value))
    # The real Slack entry end to end: comma scopes, no PKCE, client secret sent.
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", CLIENT_SECRET)
    rec = Recorder(lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_code"}))
    provider = OAuthCodeProvider(CONNECTORS["slack"], transport=rec.transport())
    url = provider.begin(store, redirect_uri="https://fabri.example.test/slack/oauth/callback",
                         scope_kind="org", handle=None, user_id=None)
    query = _query(url)
    assert "code_challenge" not in query
    assert query["scope"] == "chat:write,channels:read,app_mentions:read,channels:history"
    with pytest.raises(ConnectError, match="invalid_code"):
        provider.finish(store, state=query["state"], code=CODE)
    form = _form(rec.requests[0])
    assert form["client_secret"] == CLIENT_SECRET and "code_verifier" not in form
    assert str(rec.requests[0].url) == "https://slack.com/api/oauth.v2.access"
    assert "invalid_code" in caplog.text
    _no_secret_text(caplog.text)
    assert store.list() == []


def test_slack_scopes_match_the_service_helper():
    from fabri.service.slack_oauth import SLACK_BOT_SCOPES

    assert CONNECTORS["slack"].oauth.scopes_default == tuple(SLACK_BOT_SCOPES.split(","))


def test_linear_identity_calls_fetch_workspace(store, monkeypatch):
    seen = []

    def fake_fetch(token):
        seen.append(token)
        return {"id": "org-1", "name": "Linear Org"}

    monkeypatch.setattr(linear_oauth, "fetch_workspace", fake_fetch)
    monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", CLIENT_SECRET)
    rec = Recorder(lambda r: httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH,
                                                       "expires_in": 86399, "scope": ["read", "write"]}))
    provider = OAuthCodeProvider(CONNECTORS["linear"], transport=rec.transport())
    url = provider.begin(store, redirect_uri="https://fabri.example.test/linear/oauth/callback",
                         scope_kind="org", handle=None, user_id=None, now=T0)
    query = _query(url)
    assert query["actor"] == "application" and query["scope"] == "read,write"
    result = provider.finish(store, state=query["state"], code=CODE, now=T0)
    assert seen == [ACCESS]
    assert (result["handle"], result["display_name"], result["scopes"]) == ("org-1", "Linear Org", "read write")
    assert str(rec.requests[0].url) == "https://api.linear.app/oauth/token"
    # The legacy Linear view sees the row the generic flow wrote.
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT workspace_id FROM linear_installs").fetchall() == [("org-1",)]

    # An identity hook that fails stores nothing.
    def broken(token):
        raise RuntimeError(f"boom {token}")

    monkeypatch.setattr(linear_oauth, "fetch_workspace", broken)
    store.delete("linear", "org-1")
    url = provider.begin(store, redirect_uri="https://fabri.example.test/linear/oauth/callback",
                         scope_kind="org", handle=None, user_id=None)
    with pytest.raises(ConnectError, match=r"identity lookup failed \(RuntimeError\)") as info:
        provider.finish(store, state=_query(url)["state"], code=CODE)
    _no_secret_text(str(info.value))
    assert store.list() == []


def test_refresh_rotates_and_errors_without_access_token(monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    monkeypatch.setenv("ZZ_CLIENT_SECRET", CLIENT_SECRET)
    responses = iter([
        httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 60}),
        httpx.Response(200, json={"error": "nope"}),
        httpx.Response(401, json={"echo": REFRESH}),
    ])
    rec = Recorder(lambda request: next(responses))
    provider = OAuthCodeProvider(_pkce_entry(), transport=rec.transport())
    assert provider.refresh(REFRESH)["refresh_token"] == "r2"
    assert _form(rec.requests[0]) == {"grant_type": "refresh_token", "refresh_token": REFRESH,
                                      "client_id": "cid", "client_secret": CLIENT_SECRET}
    with pytest.raises(RuntimeError, match="no access_token"):
        provider.refresh(REFRESH)
    with pytest.raises(RuntimeError, match="HTTP 401") as info:
        provider.refresh(REFRESH)
    _no_secret_text(str(info.value))
    monkeypatch.delenv("ZZ_CLIENT_ID")
    with pytest.raises(RuntimeError, match="ZZ_CLIENT_ID"):
        provider.refresh(REFRESH)


def test_refresh_through_the_store_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("ZZ_CLIENT_ID", "cid")
    rec = Recorder(lambda r: httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2",
                                                       "expires_in": 3600}))
    provider = OAuthCodeProvider(_pkce_entry(), transport=rec.transport())
    store = ConnectionStore(tmp_path / "installs.db", refreshers={"zz": provider.refresh})
    store.upsert(provider="zz", handle="main", access_token="a1", refresh_token="r1", expires_in=60, now=T0)
    assert store.get_token("zz", "main", now=T0 + 30) == "a2"
    assert _form(rec.requests[0])["refresh_token"] == "r1"
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT refresh_token FROM connections").fetchone() == ("r2",)


def test_default_refreshers_covers_every_oauth_entry(monkeypatch):
    refreshers = cs_module.default_refreshers()
    expected = {e.secret_provider or n for n, e in CONNECTORS.items() if e.oauth and e.oauth.refresh}
    assert set(refreshers) == expected
    assert "linear" in refreshers and "slack" not in refreshers
    # Linear keeps the patched legacy helper so existing hosts see one refresher.
    monkeypatch.setattr(linear_oauth, "refresh_access_token", lambda r: {"access_token": "x"})
    assert cs_module.default_refreshers()["linear"]("r") == {"access_token": "x"}
    # A new OAuth entry with refresh gets the generic provider's refresh.
    zz = replace(_pkce_entry(), name="zz", secret_provider="zz")
    monkeypatch.setitem(CONNECTORS, "zz", zz)
    refresher = cs_module.default_refreshers()["zz"]
    assert refresher.__self__.entry is zz


def test_state_secret_env_wins_else_persisted_once(tmp_path, monkeypatch):
    db = tmp_path / "installs.db"
    monkeypatch.setenv(STATE_SECRET_ENV, "pinned-secret")
    store = ConnectionStore(db)
    assert store.state_secret() == "pinned-secret"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM service_secrets").fetchone() == (0,)
    monkeypatch.delenv(STATE_SECRET_ENV)
    first = store.state_secret()
    assert first and first != "pinned-secret"
    assert ConnectionStore(db).state_secret() == first
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM service_secrets").fetchone() == (1,)


def test_scope_separator_comma_for_slack_and_linear():
    assert CONNECTORS["slack"].oauth.scope_separator == ","
    assert CONNECTORS["linear"].oauth.scope_separator == ","
    assert OAuthSpec("https://a.test", "https://b.test", ("x",)).scope_separator == " "
    assert CONNECTORS["slack"].oauth.callback_alias == "/slack/oauth/callback"
    assert CONNECTORS["linear"].oauth.callback_alias == "/linear/oauth/callback"
    assert CONNECTORS["linear"].oauth.extra_authorize == {"actor": "application"}
    assert CONNECTORS["slack"].oauth.refresh is False and CONNECTORS["slack"].oauth.pkce is False


def test_list_connectors_oauth_fields(monkeypatch):
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    rows = {r["name"]: r for r in list_connectors()}
    assert rows["slack"]["oauth"] is True and rows["slack"]["oauth_configured"] is False
    assert rows["slack"]["oauth_loopback"] is False and rows["github"]["oauth"] is False
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", CLIENT_SECRET)
    rows = {r["name"]: r for r in list_connectors()}
    assert rows["slack"]["oauth_configured"] is True
    assert all("token" not in k and "secret" not in k for r in rows.values() for k in r)
    assert CLIENT_SECRET not in repr(list_connectors())
