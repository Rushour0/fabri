"""/connections and /connect/<provider> routes, the Slack and Linear aliases,
and who may use them under each auth mode."""
from __future__ import annotations

import base64
import hashlib
import logging
import socket
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fabri.service import linear_oauth
from fabri.service.auth import sign_session
from fabri.service.connections_routes import ConnectionsApi
from fabri.service.http_server import _ADMIN_GETS, StartRefused, _Handler, serve_http
from fabri.service.service import FabriService
from fabri.tools import connector_catalog
from fabri.tools.connector_catalog import Connector, OAuthSpec

pytestmark = pytest.mark.unit

# A loopback base: most tests run with auth off, and serve_http refuses a
# hosted (non-loopback) base with auth off. HOSTED is for the auth-on cases.
BASE = "http://127.0.0.1:8080"
HOSTED = "https://fabri.example.test"
ACME_TOKEN_URL = "https://acme.example.test/oauth/token"
ACME_ACCESS = "acme-access-value-never-shown-0001"
ACME_REFRESH = "acme-refresh-value-never-shown-0001"
SLACK_ACCESS = "xoxb-slack-access-value-never-shown"
LINEAR_ACCESS = "lin-access-value-never-shown-0001"
CLIENT_SECRET = "client-secret-value-never-shown"
CODE = "auth-code-value-never-shown"
PASTED = "pasted-token-value-never-shown-0001"

BOSS, DEV = "boss@x.test", "dev@x.test"
INVITE_ONLY = f"""auth:
  enabled: true
  secure_cookie: false
  signup: true
  admins: [{BOSS}]
  allowed_emails: [{BOSS}, {DEV}]
"""
GUESTS = INVITE_ONLY + "  allow_guests: true\n"

ACME = Connector(
    "acme", "Acme", "A test connector with a PKCE OAuth flow.", ("http",), "ACME_TOKEN", "acme",
    "https://api.acme.example.test",
    oauth=OAuthSpec("https://acme.example.test/oauth/authorize", ACME_TOKEN_URL, ("read",),
                    client_id_env="ACME_CLIENT_ID", pkce=True))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN", "FABRI_AUTH_ALLOWED_EMAILS",
                 "FABRI_AUTH_ADMINS", "FABRI_AUTH_SIGNUP", "FABRI_AUTH_DB_PATH", "FABRI_AUTH_ENABLED",
                 "FABRI_AUTH_CONFIG", "FABRI_INSTALL_DB", "FABRI_OAUTH_STATE_SECRET",
                 "FABRI_FETCH_ALLOW_PRIVATE", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET",
                 "SLACK_SIGNING_SECRET", "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "ACME_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_AUTH_SECRET", "secret")
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", BASE)
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host in ("127.0.0.1", "localhost", "::1"):
            return real(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setitem(connector_catalog.CONNECTORS, "acme", ACME)
    monkeypatch.setitem(connector_catalog._MODULES, "acme",
                        SimpleNamespace(ENTRY=ACME, identity=lambda tok, get_json: ("acct-1", "Acme Co")))
    monkeypatch.setenv("ACME_CLIENT_ID", "acme-client")


@pytest.fixture
def vendor():
    """Fake token endpoints. `state["seen"]` records every request."""
    state: dict = {"seen": [], "slack_ok": True, "verify_status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        state["seen"].append(request)
        url = str(request.url)
        if url == ACME_TOKEN_URL:
            return httpx.Response(200, json={"access_token": ACME_ACCESS, "refresh_token": ACME_REFRESH,
                                             "expires_in": 3600, "scope": "read"})
        if url == "https://slack.com/api/oauth.v2.access":
            if not state["slack_ok"]:
                return httpx.Response(200, json={"ok": False, "error": "invalid_code",
                                                 "access_token": SLACK_ACCESS})
            return httpx.Response(200, json={"ok": True, "access_token": SLACK_ACCESS,
                                             "team": {"id": "T1", "name": "Acme"}, "scope": "chat:write"})
        if url == "https://api.linear.app/oauth/token":
            return httpx.Response(200, json={"access_token": LINEAR_ACCESS, "refresh_token": "lin-r",
                                             "expires_in": 86400, "scope": "read write"})
        if url.startswith("https://api.cakemail.dev/"):
            return httpx.Response(state["verify_status"], json={"data": {"email": "ops@example.com"}})
        return httpx.Response(404)

    state["transport"] = httpx.MockTransport(handler)
    return state


@pytest.fixture
def make_server(tmp_path, vendor):
    started = []

    def make(auth_body: str | None = None, *, serve_studio: bool = False):
        kwargs = {}
        if auth_body is not None:
            path = tmp_path / "fabri-service.yaml"
            path.write_text(auth_body)
            kwargs["auth_config"] = path
        service = FabriService(home_root=tmp_path / "home", **kwargs)
        server = serve_http(service, port=0, serve_studio=serve_studio,
                            connections_api=ConnectionsApi(service, transport=vendor["transport"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, service))
        return server, service

    yield make
    for server, service in started:
        server.shutdown()
        server.server_close()
        service.close()


SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def client(server, headers=None) -> httpx.Client:
    """A client that looks like Studio in a browser: same-origin requests."""
    return httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                        headers={**SAME_ORIGIN, **(headers or {})}, follow_redirects=False)


def bare(server, headers=None) -> httpx.Client:
    """A client with no browser headers at all (curl, a script)."""
    return httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                        headers=headers or {}, follow_redirects=False)


def session(service, email) -> tuple[dict, str]:
    users = service.user_store
    user_id = users.verify_user(email, "password-1") or users.create_user(email, "password-1")
    return {"Cookie": f"fabri_session={sign_session(user_id, 'secret', 60)}"}, user_id


def location(resp: httpx.Response) -> tuple[str, dict[str, str]]:
    assert resp.status_code == 302, resp.text
    parts = urlsplit(resp.headers["location"])
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.path, {
        k: v[0] for k, v in parse_qs(parts.query).items()}


def token_posts(vendor, url: str) -> list[httpx.Request]:
    return [r for r in vendor["seen"] if str(r.url) == url]


def assert_clean(text: str) -> None:
    for secret in (ACME_ACCESS, ACME_REFRESH, SLACK_ACCESS, LINEAR_ACCESS, CLIENT_SECRET, CODE, PASTED):
        assert secret not in text


# ---- /connect/<provider>


def test_connect_redirect_carries_state_and_pkce(make_server, vendor):
    server, service = make_server()
    target, query = location(client(server).get("/connect/acme"))
    assert target == "https://acme.example.test/oauth/authorize"
    assert query["client_id"] == "acme-client"
    assert query["redirect_uri"] == f"{BASE}/connect/acme/callback"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["state"]

    resp = client(server).get("/connect/acme/callback", params={"state": query["state"], "code": CODE})
    assert location(resp)[0] == "/"
    (exchange,) = token_posts(vendor, ACME_TOKEN_URL)
    form = parse_qs(exchange.content.decode())
    verifier = form["code_verifier"][0]
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert digest == query["code_challenge"]
    assert form["redirect_uri"] == [f"{BASE}/connect/acme/callback"]
    assert "client_secret" not in form


def test_connect_unconfigured_is_500_without_secrets_in_body(make_server, monkeypatch):
    server, _ = make_server()
    monkeypatch.setenv("SLACK_CLIENT_ID", "slack-client-id-value")
    resp = client(server).get("/connect/slack")
    assert resp.status_code == 500
    assert resp.json() == {"error": "slack OAuth is not configured"}

    monkeypatch.setenv("SLACK_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", "")
    resp = client(server).get("/connect/slack")
    assert resp.status_code == 500
    assert resp.json() == {"error": "slack OAuth is not configured"}
    assert_clean(resp.text)
    assert "slack-client-id-value" not in resp.text


def test_connect_unknown_and_token_only_providers(make_server):
    server, _ = make_server()
    c = client(server)
    assert c.get("/connect/nope").status_code == 404
    resp = c.get("/connect/cakemail")
    assert resp.status_code == 400
    assert resp.json() == {"error": "cakemail connects with a token: POST /connections"}
    assert c.get("/connect/acme?scope=team").status_code == 400


def test_callback_stores_row_and_redirects_connected(make_server, vendor):
    server, service = make_server()
    c = client(server)
    _, query = location(c.get("/connect/acme"))
    resp = c.get("/connect/acme/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"] == "/?connect=acme&status=connected#settings"
    row = service.connection_store.get("acme", "acct-1")
    assert row["scope_kind"] == "org"
    assert row["display_name"] == "Acme Co"
    assert row["expires_at"] is not None
    assert service.connection_store.get_token("acme", "acct-1") == ACME_ACCESS
    listing = c.get("/connections")
    assert listing.status_code == 200
    assert [(r["provider"], r["handle"]) for r in listing.json()["connections"]] == [("acme", "acct-1")]
    assert_clean(listing.text)


def test_callback_error_query_and_missing_code_redirect_with_error(make_server, vendor):
    server, service = make_server()
    c = client(server)
    _, query = location(c.get("/connect/acme"))
    resp = c.get("/connect/acme/callback", params={"state": query["state"], "error": "access_denied"})
    assert resp.headers["location"] == "/?connect=acme&status=error#settings"
    resp = c.get("/connect/acme/callback", params={"state": query["state"]})
    assert resp.headers["location"] == "/?connect=acme&status=error#settings"
    assert token_posts(vendor, ACME_TOKEN_URL) == []
    # The pending row was left to expire, so the right callback still works.
    resp = c.get("/connect/acme/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"] == "/?connect=acme&status=connected#settings"


def test_callback_single_use(make_server, vendor):
    server, service = make_server()
    c = client(server)
    _, query = location(c.get("/connect/acme"))
    params = {"state": query["state"], "code": CODE}
    assert c.get("/connect/acme/callback", params=params).headers["location"].endswith("status=connected#settings")
    assert c.get("/connect/acme/callback", params=params).headers["location"].endswith("status=error#settings")
    assert len(token_posts(vendor, ACME_TOKEN_URL)) == 1


def test_callback_rejects_principal_mismatch(make_server, vendor):
    server, service = make_server(INVITE_ONLY)
    dev_headers, dev_id = session(service, DEV)
    boss_headers, _ = session(service, BOSS)
    dev, boss, anon = client(server, dev_headers), client(server, boss_headers), client(server)

    # A user flow dev started cannot be finished in boss's browser. The
    # refused callers leave the pending row alone, so dev still can.
    _, query = location(dev.get("/connect/acme?scope=user"))
    params = {"state": query["state"], "code": CODE}
    assert anon.get("/connect/acme/callback", params=params).headers["location"].endswith("status=error#settings")
    assert boss.get("/connect/acme/callback", params=params).headers["location"].endswith("status=error#settings")
    assert token_posts(vendor, ACME_TOKEN_URL) == []
    assert service.connection_store.list() == []
    assert dev.get("/connect/acme/callback", params=params).headers["location"].endswith("status=connected#settings")
    assert len(token_posts(vendor, ACME_TOKEN_URL)) == 1
    service.connection_store.delete("acme", dev_id)

    # An org flow boss started cannot be finished by dev, and boss still can.
    _, query = location(boss.get("/connect/acme?scope=org"))
    params = {"state": query["state"], "code": CODE}
    assert dev.get("/connect/acme/callback", params=params).headers["location"].endswith("status=error#settings")
    assert len(token_posts(vendor, ACME_TOKEN_URL)) == 1
    assert boss.get("/connect/acme/callback", params=params).headers["location"].endswith("status=connected#settings")
    assert len(token_posts(vendor, ACME_TOKEN_URL)) == 2
    service.connection_store.delete("acme", "acct-1")

    # The same principal finishes its own user flow, under its own user id.
    _, query = location(dev.get("/connect/acme?scope=user"))
    params = {"state": query["state"], "code": CODE}
    assert dev.get("/connect/acme/callback", params=params).headers["location"].endswith("status=connected#settings")
    row = service.connection_store.get("acme", dev_id)
    assert row["scope_kind"] == "user"
    assert service.connection_store.list(handle="acct-1") == []


def test_connect_scope_rules_under_strict_auth(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    server, service = make_server(INVITE_ONLY)
    dev_headers, _ = session(service, DEV)
    dev = client(server, dev_headers)
    assert client(server).get("/connect/acme").status_code == 401
    assert dev.get("/connect/acme?scope=org").status_code == 403
    assert dev.get("/slack/install").status_code == 403
    token = client(server, {"Authorization": "Bearer adm"})
    resp = token.get("/connect/acme?scope=user")
    assert resp.status_code == 400
    assert "signed-in user" in resp.json()["error"]


def test_connect_user_scope_with_auth_off_is_400(make_server):
    server, _ = make_server()
    resp = client(server).get("/connect/acme?scope=user")
    assert resp.status_code == 400
    assert resp.json() == {"error": "user-scoped connections need a signed-in user"}


def test_guest_mode_needs_admin_for_org_and_a_user_for_user(make_server):
    server, service = make_server(GUESTS)
    dev_headers, dev_id = session(service, DEV)
    boss_headers, _ = session(service, BOSS)
    dev = client(server, dev_headers)
    assert client(server).get("/connect/acme?scope=user").status_code == 401
    assert dev.get("/connect/acme?scope=org").status_code == 403
    assert location(client(server, boss_headers).get("/connect/acme?scope=org"))[0].startswith("https://")
    _, query = location(dev.get("/connect/acme?scope=user"))
    resp = dev.get("/connect/acme/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"].endswith("status=connected#settings")
    assert service.connection_store.scope_kind("acme", dev_id) == "user"


# ---- Slack and Linear aliases


def test_slack_alias_paths_still_work_and_redirect_with_slack_query(make_server, vendor, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_CLIENT_ID", "slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", CLIENT_SECRET)
    server, service = make_server()
    c = client(server)
    target, query = location(c.get("/slack/install"))
    assert target == "https://slack.com/oauth/v2/authorize"
    assert query["redirect_uri"] == f"{BASE}/slack/oauth/callback"
    assert query["scope"] == "chat:write,channels:read,app_mentions:read,channels:history"
    assert "code_challenge" not in query

    resp = c.get("/slack/oauth/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"] == "/?slack=connected#settings"
    (exchange,) = token_posts(vendor, "https://slack.com/api/oauth.v2.access")
    assert parse_qs(exchange.content.decode())["redirect_uri"] == [f"{BASE}/slack/oauth/callback"]
    assert service.install_store.get_token("T1") == SLACK_ACCESS
    installs = c.get("/slack/installs").json()["installs"]
    assert [(i["team_id"], i["team_name"]) for i in installs] == [("T1", "Acme")]

    # A refused exchange logs Slack's error code only.
    vendor["slack_ok"] = False
    caplog.set_level(logging.WARNING)
    _, query = location(c.get("/slack/install"))
    resp = c.get("/slack/oauth/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"] == "/?slack=error#settings"
    assert "invalid_code" in caplog.text
    assert_clean(caplog.text)

    # A bad state never reaches Slack.
    before = len(vendor["seen"])
    resp = c.get("/slack/oauth/callback", params={"state": "forged.1.sig", "code": CODE})
    assert resp.headers["location"] == "/?slack=error#settings"
    assert len(vendor["seen"]) == before


def test_linear_alias_paths(make_server, vendor, monkeypatch):
    monkeypatch.setenv("LINEAR_CLIENT_ID", "linear-client")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", CLIENT_SECRET)
    seen_tokens = []

    def fetch_workspace(access_token):
        seen_tokens.append(access_token)
        return {"id": "W1", "name": "Lin"}

    monkeypatch.setattr(linear_oauth, "fetch_workspace", fetch_workspace)
    server, service = make_server()
    c = client(server)
    target, query = location(c.get("/linear/install"))
    assert target == "https://linear.app/oauth/authorize"
    assert query["actor"] == "application"
    assert query["redirect_uri"] == f"{BASE}/linear/oauth/callback"
    assert query["scope"] == "read,write"

    resp = c.get("/linear/oauth/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"] == "/?linear=connected#settings"
    assert seen_tokens == [LINEAR_ACCESS]
    assert service.linear_install_store.get("W1")["workspace_name"] == "Lin"
    assert service.linear_install_store.get_token("W1") == LINEAR_ACCESS
    form = parse_qs(token_posts(vendor, "https://api.linear.app/oauth/token")[0].content.decode())
    assert form["client_secret"] == [CLIENT_SECRET]

    resp = c.get("/linear/oauth/callback", params={"error": "access_denied"})
    assert resp.headers["location"] == "/?linear=error#settings"


# ---- /connections


def _seed(service, dev_id, boss_id):
    store = service.connection_store
    store.upsert(provider="cakemail", handle="main", access_token="t-org")
    store.upsert(provider="acme", handle=dev_id, access_token="t-dev", scope_kind="user")
    store.upsert(provider="acme", handle=boss_id, access_token="t-boss", scope_kind="user")


def test_connections_list_scopes_rows_per_user(make_server):
    server, service = make_server(INVITE_ONLY)
    dev_headers, dev_id = session(service, DEV)
    boss_headers, boss_id = session(service, BOSS)
    _seed(service, dev_id, boss_id)

    assert client(server).get("/connections").status_code == 401
    rows = client(server, dev_headers).get("/connections").json()["connections"]
    assert {(r["handle"], r["mine"]) for r in rows} == {("main", False), (dev_id, True)}
    rows = client(server, boss_headers).get("/connections").json()["connections"]
    assert {(r["handle"], r["mine"]) for r in rows} == {("main", False), (dev_id, False), (boss_id, True)}
    for row in rows:
        assert not {"access_token", "refresh_token"} & set(row)


def test_connections_list_with_auth_off_honours_the_service_token(make_server, monkeypatch):
    server, service = make_server()
    service.connection_store.upsert(provider="cakemail", handle="main", access_token="t-org")
    assert [r["handle"] for r in client(server).get("/connections").json()["connections"]] == ["main"]
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc")
    assert client(server).get("/connections").status_code == 401
    assert client(server, {"Authorization": "Bearer svc"}).get("/connections").status_code == 200


def test_connections_post_static_token_admin_only_for_org(make_server, vendor):
    server, service = make_server(INVITE_ONLY)
    dev_headers, _ = session(service, DEV)
    boss_headers, _ = session(service, BOSS)
    body = {"provider": "cakemail", "token": PASTED, "verify": False}

    assert client(server).post("/connections", json=body).status_code == 401
    assert client(server, dev_headers).post("/connections", json=body).status_code == 403
    assert service.connection_store.list() == []

    resp = client(server, boss_headers).post("/connections", json=body)
    assert resp.status_code == 200
    assert resp.json()["connection"]["handle"] == "main"
    assert resp.json()["connection"]["scope_kind"] == "org"
    assert service.connection_store.get_token("cakemail", "main") == PASTED
    assert_clean(resp.text)

    # A refused token stores nothing and is not echoed.
    vendor["verify_status"] = 401
    resp = client(server, boss_headers).post(
        "/connections", json={"provider": "cakemail", "handle": "second", "token": PASTED})
    assert resp.status_code == 400
    assert resp.json() == {"error": "cakemail refused the token"}
    assert service.connection_store.get("cakemail", "second") is None

    assert client(server, boss_headers).post(
        "/connections", json={"provider": "nope", "token": PASTED}).status_code == 404


def test_connections_post_with_auth_off(make_server, monkeypatch):
    server, service = make_server()
    body = {"provider": "cakemail", "handle": "ops", "token": PASTED, "verify": False}
    assert client(server).post("/connections", json=body).status_code == 200
    resp = client(server).post("/connections", json={**body, "scope": "user"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "user-scoped connections need a signed-in user"}
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    assert client(server).post("/connections", json=body).status_code == 401
    assert client(server, {"Authorization": "Bearer adm"}).post("/connections", json=body).status_code == 200


def test_connections_post_user_scope_needs_user_and_forces_handle(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    server, service = make_server(INVITE_ONLY)
    dev_headers, dev_id = session(service, DEV)
    body = {"provider": "cakemail", "handle": "someone-else", "token": PASTED, "scope": "user",
            "verify": False}

    resp = client(server, dev_headers).post("/connections", json=body)
    assert resp.status_code == 200
    row = resp.json()["connection"]
    assert (row["handle"], row["scope_kind"], row["mine"]) == (dev_id, "user", True)
    assert service.connection_store.get("cakemail", "someone-else") is None

    resp = client(server, {"Authorization": "Bearer adm"}).post("/connections", json=body)
    assert resp.status_code == 400
    assert "signed-in user" in resp.json()["error"]

    # An org row with the user's id as handle is not taken over.
    service.connection_store.delete("cakemail", dev_id)
    service.connection_store.upsert(provider="cakemail", handle=dev_id, access_token="t-org")
    resp = client(server, dev_headers).post("/connections", json=body)
    assert resp.status_code == 409
    assert service.connection_store.get_token("cakemail", dev_id) == "t-org"


def test_connections_delete_owner_or_admin(make_server):
    server, service = make_server(INVITE_ONLY)
    dev_headers, dev_id = session(service, DEV)
    boss_headers, boss_id = session(service, BOSS)
    _seed(service, dev_id, boss_id)
    dev, boss = client(server, dev_headers), client(server, boss_headers)
    store = service.connection_store

    resp = dev.delete("/connections", params={"provider": "acme", "handle": boss_id})
    assert resp.json() == {"deleted": False}
    assert store.get("acme", boss_id) is not None
    assert dev.delete("/connections", params={"provider": "cakemail", "handle": "main"}).status_code == 403
    assert store.get("cakemail", "main") is not None
    assert dev.delete("/connections", params={"provider": "acme", "handle": dev_id}).json() == {"deleted": True}

    assert boss.delete("/connections", params={"provider": "acme", "handle": boss_id}).json() == {"deleted": True}
    assert boss.delete("/connections", params={"provider": "cakemail", "handle": "main"}).json() == {"deleted": True}
    assert boss.delete("/connections", params={"provider": "cakemail", "handle": "main"}).json() == {"deleted": False}
    assert boss.delete("/connections", params={"provider": "cakemail"}).status_code == 400
    assert store.list() == []


def test_connections_body_never_echoes_token(make_server, caplog):
    caplog.set_level(logging.DEBUG)
    server, service = make_server()
    c = client(server)
    texts = [
        c.post("/connections", json={"provider": "cakemail", "token": PASTED, "verify": False}).text,
        c.post("/connections", content=b'{"provider": "cakemail", "token": "' + PASTED.encode() + b'"',
               headers={"Content-Type": "application/json"}).text,
        c.post("/connections", json={"provider": "cakemail", "handle": "bad handle!", "token": PASTED}).text,
        c.post("/connections", json=[PASTED]).text,
        c.get("/connections").text,
    ]
    for text in texts:
        assert_clean(text)
    assert_clean(caplog.text)


# ---- cross-site requests


def _pending_count(service) -> int:
    import sqlite3

    with sqlite3.connect(service.connection_store.path) as c:
        count = c.execute("SELECT COUNT(*) FROM pending_oauth").fetchone()[0]
    c.close()
    return count


def test_auth_off_mutations_refuse_cross_site_requests(make_server, monkeypatch):
    server, service = make_server()
    port = server.server_address[1]
    store = service.connection_store
    body = {"provider": "cakemail", "handle": "ops", "token": PASTED, "verify": False}
    raw = b'{"provider": "cakemail", "handle": "ops", "token": "' + PASTED.encode() + b'", "verify": false}'

    # A CORS-simple text/plain POST, the shape a malicious page can send.
    resp = bare(server, SAME_ORIGIN).post("/connections", content=raw, headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert bare(server).post("/connections", content=raw,
                             headers={"Content-Type": "text/plain"}).status_code == 415
    # Another origin, an opaque origin, and Sec-Fetch-Site: cross-site.
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Origin": f"http://127.0.0.1:{port + 1}"}):
        resp = bare(server, headers).post("/connections", json=body)
        assert resp.status_code == 403, headers
        assert_clean(resp.text)
    # No browser headers and no token: not shown to be same-origin.
    assert bare(server).post("/connections", json=body).status_code == 403
    # A DNS-rebound page: same-origin to the browser, but under its own host name.
    rebound = {"Host": f"evil.example:{port}", "Sec-Fetch-Site": "same-origin"}
    assert bare(server, rebound).post("/connections", json=body).status_code == 403
    rebound_origin = {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"}
    assert bare(server, rebound_origin).post("/connections", json=body).status_code == 403
    assert store.list() == []

    # The service's own origin, or Studio's same-origin fetch, is accepted.
    assert bare(server, {"Origin": f"http://127.0.0.1:{port}"}).post("/connections", json=body).status_code == 200
    assert bare(server, {"Origin": BASE}).post("/connections", json=body).status_code == 200
    assert client(server).post("/connections", json=body).status_code == 200

    # DELETE follows the same rules.
    gone = {"provider": "cakemail", "handle": "ops"}
    assert bare(server, {"Sec-Fetch-Site": "cross-site"}).delete("/connections", params=gone).status_code == 403
    assert bare(server, {"Origin": "https://evil.example"}).delete("/connections", params=gone).status_code == 403
    assert bare(server).delete("/connections", params=gone).status_code == 403
    assert store.get("cakemail", "ops") is not None

    # A token stands in for the same-origin proof, but not for a cross-site request.
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    token = {"Authorization": "Bearer adm"}
    assert bare(server, token).post("/connections", json=body).status_code == 200
    assert bare(server, {**token, "Sec-Fetch-Site": "cross-site"}).post(
        "/connections", json=body).status_code == 403
    assert bare(server, token).delete("/connections", params=gone).json() == {"deleted": True}


def test_auth_off_connect_start_refuses_cross_site_pages(make_server, monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", CLIENT_SECRET)
    server, service = make_server()
    for path in ("/connect/acme", "/slack/install", "/linear/install"):
        assert bare(server, {"Sec-Fetch-Site": "cross-site"}).get(path).status_code == 403, path
        assert bare(server, {"Sec-Fetch-Site": "same-site"}).get(path).status_code == 403, path
        assert bare(server, {"Origin": "https://evil.example"}).get(path).status_code == 403, path
        assert bare(server).get(path).status_code == 403, path
    assert _pending_count(service) == 0
    # Studio's link (same-origin) and a typed URL (none) start the flow.
    assert location(client(server).get("/connect/acme"))[0].startswith("https://acme.example.test/")
    assert location(bare(server, {"Sec-Fetch-Site": "none"}).get("/slack/install"))[0].startswith(
        "https://slack.com/")
    assert _pending_count(service) == 2


def test_auth_on_refuses_cross_site_even_for_an_admin(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", HOSTED)
    server, service = make_server(INVITE_ONLY)
    boss_headers, _ = session(service, BOSS)
    body = {"provider": "cakemail", "token": PASTED, "verify": False}
    cross = {**boss_headers, "Sec-Fetch-Site": "cross-site"}
    assert bare(server, cross).get("/connect/acme").status_code == 403
    assert bare(server, cross).post("/connections", json=body).status_code == 403
    assert bare(server, {**boss_headers, "Origin": "https://evil.example"}).post(
        "/connections", json=body).status_code == 403
    assert bare(server, boss_headers).post("/connections", content=b"{}",
                                           headers={"Content-Type": "text/plain"}).status_code == 415
    assert service.connection_store.list() == []
    # The public base URL's origin is the service's own; auth on needs no other proof.
    assert bare(server, {**boss_headers, "Origin": HOSTED}).post("/connections", json=body).status_code == 200
    assert bare(server, boss_headers).post("/connections", json=body).status_code == 200
    target, query = location(bare(server, boss_headers).get("/connect/acme"))
    assert query["redirect_uri"] == f"{HOSTED}/connect/acme/callback"


def test_callbacks_accept_the_vendor_navigation_and_send_no_referrer(make_server, vendor):
    server, service = make_server()
    _, query = location(client(server).get("/connect/acme"))
    vendor_nav = bare(server, {"Sec-Fetch-Site": "cross-site"})
    resp = vendor_nav.get("/connect/acme/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"] == "/?connect=acme&status=connected#settings"
    assert resp.headers["referrer-policy"] == "no-referrer"
    resp = vendor_nav.get("/connect/acme/callback", params={"state": query["state"], "code": CODE})
    assert resp.headers["location"].endswith("status=error#settings")
    assert resp.headers["referrer-policy"] == "no-referrer"
    resp = vendor_nav.get("/slack/oauth/callback", params={"error": "access_denied"})
    assert resp.headers["referrer-policy"] == "no-referrer"
    resp = vendor_nav.get("/connect/nope/callback", params={"state": "s", "code": CODE})
    assert resp.status_code == 404
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert_clean(resp.text)


def test_callback_query_strings_never_reach_the_log(make_server, vendor, caplog):
    caplog.set_level(logging.DEBUG)
    server, service = make_server()
    _, query = location(client(server).get("/connect/acme"))
    state = query["state"]
    for path in ("/connect/acme/callback", "/slack/oauth/callback", "/linear/oauth/callback"):
        client(server).get(path, params={"state": state, "code": CODE})
    # A malformed request line goes through log_message with the raw line.
    with socket.create_connection(("127.0.0.1", server.server_address[1])) as sock:
        sock.sendall(f"GET /connect/acme/callback?code={CODE}&state={state} x HTTP/1.1\r\n\r\n".encode())
        sock.recv(4096)
    # The server's own log lines (the test client logs its URLs separately).
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.split(".")[0] == "fabri")
    assert "GET /connect/acme/callback 302" in text
    assert "Bad request syntax" in text and "[query removed]" in text
    assert CODE not in text
    assert state not in text


# ---- startup


def test_serve_refuses_a_hosted_base_with_auth_off(tmp_path, monkeypatch):
    service = FabriService(home_root=tmp_path / "home")
    try:
        monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", HOSTED)
        with pytest.raises(StartRefused, match="auth is off") as caught:
            serve_http(service, port=0)
        assert caught.value.code.startswith("fabri serve refused to start: ")
        for bad, why in (("http://fabri.example.test", "must use https"),
                         ("ftp://fabri.example.test", "http or https"),
                         ("fabri.example.test", "http or https"),
                         ("https://fabri.example.test/?x=1", "plain origin")):
            monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", bad)
            with pytest.raises(StartRefused, match=why):
                serve_http(service, port=0)
        for good in ("http://127.0.0.1:8080", "http://localhost:8080/", "http://[::1]:9000", ""):
            monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", good)
            serve_http(service, port=0).server_close()
    finally:
        service.close()


def test_serve_starts_hosted_with_auth_on(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", HOSTED)
    server, _ = make_server(INVITE_ONLY)
    assert client(server).get("/health").status_code == 200
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", "http://fabri.example.test")
    with pytest.raises(StartRefused, match="must use https"):
        make_server(INVITE_ONLY)


# ---- mounting


def test_studio_mode_does_not_serve_index_for_connect_paths(make_server):
    server, service = make_server(serve_studio=True)
    resp = client(server).get("/connections")
    assert resp.status_code == 200
    assert resp.json() == {"connections": []}
    resp = client(server).get("/connect/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/json"
    for path in ("/connect", "/connect/slack", "/connect/slack/callback", "/connections"):
        assert _Handler._is_api_path(path)


def test_admin_gets_no_longer_lists_legacy_paths():
    for path in ("/slack/install", "/slack/oauth/callback", "/linear/install", "/linear/oauth/callback"):
        assert path not in _ADMIN_GETS
        assert ConnectionsApi(SimpleNamespace()).matches(path)
    assert not ConnectionsApi(SimpleNamespace()).matches("/slack/installs")
    assert not ConnectionsApi(SimpleNamespace()).matches("/connect/slack/other")


def test_serve_http_mounts_a_default_connections_api(tmp_path):
    service = FabriService(home_root=tmp_path / "home")
    try:
        server = serve_http(service, port=0)
        assert isinstance(server.connections_api, ConnectionsApi)
        server.server_close()
    finally:
        service.close()


def test_hosted_install_without_key_refuses_oauth_and_paste(make_server, vendor, monkeypatch):
    from fabri import secret_cipher

    monkeypatch.delenv(secret_cipher.SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(secret_cipher.ALLOW_PLAINTEXT_ENV, raising=False)
    server, service = make_server()
    secret_cipher.require_encryption("fabri serve is bound to the non-loopback address 0.0.0.0")
    try:
        resp = client(server).get("/connect/acme")
        assert resp.status_code == 503 and "FABRI_SECRET_KEY" in resp.text
        assert service.connection_store.list() == []
    finally:
        secret_cipher.require_encryption(None)
