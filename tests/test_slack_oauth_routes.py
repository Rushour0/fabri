from __future__ import annotations

import logging
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fabri.service.connection_store import ConnectionStore
from fabri.service.connections_routes import ConnectionsApi
from fabri.service.http_server import _Handler, _SLACK_UNINSTALL_RE
from fabri.service.install_store import SlackInstallStore

pytestmark = pytest.mark.unit


class _InstallStore:
    def __init__(self, rows: list[dict] | None = None, *, delete_result: bool = True) -> None:
        self.rows = rows or []
        self.delete_result = delete_result
        self.upsert_calls: list[dict] = []
        self.delete_calls: list[str] = []

    def upsert(self, **kwargs) -> None:
        self.upsert_calls.append(kwargs)

    def list(self) -> list[dict]:
        return self.rows

    def delete(self, team_id: str) -> bool:
        self.delete_calls.append(team_id)
        return self.delete_result


def _handler(path: str, store: _InstallStore | None = None) -> _Handler:
    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(
        service=SimpleNamespace(
            auth_enabled=False,
            install_store=store or _InstallStore(),
        ),
        serve_studio=False,
    )
    handler.path = path
    return handler


SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
BASE = "https://fabri.example.test"


@pytest.fixture
def slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN", "FABRI_OAUTH_STATE_SECRET",
                 "FABRI_FETCH_ALLOW_PRIVATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLACK_CLIENT_ID", "slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "client-secret-never-log")
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])


class _Slack:
    """A fake Slack token endpoint that records every request."""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.seen: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if str(request.url) == SLACK_TOKEN_URL:
            return httpx.Response(200, json=self.body)
        return httpx.Response(404)


def _alias_handler(path: str, store: ConnectionStore, transport=None) -> tuple[_Handler, list, list]:
    """A handler wired to a ConnectionsApi over a real store. Returns it with
    the lists that collect (status, headers) and JSON responses."""
    service = SimpleNamespace(auth_enabled=False, connection_store=store)
    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(service=service, serve_studio=False,
                                     connections_api=ConnectionsApi(service, transport=transport))
    handler.path = path
    # A same-origin browser request to a loopback fabri, as Studio sends.
    handler.headers = {"Host": "127.0.0.1:8080", "Sec-Fetch-Site": "same-origin"}
    sent: list = []
    json_sent: list = []
    handler.send_response = lambda code: sent.append(("status", code))
    handler.send_header = lambda name, value: sent.append((name, value))
    handler.end_headers = lambda: None
    handler._send_json = lambda code, payload, **kwargs: json_sent.append((code, payload))
    return handler, sent, json_sent


def _start(store: ConnectionStore) -> str:
    """Run GET /slack/install and return the signed state from the redirect."""
    handler, sent, _ = _alias_handler("/slack/install", store)
    handler.do_GET()
    location = dict(sent)["Location"]
    return parse_qs(urlsplit(location).query)["state"][0]


def test_slack_install_redirect_and_unconfigured_response(
    tmp_path, slack_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConnectionStore(tmp_path / "installs.db")
    handler, sent, _ = _alias_handler("/slack/install", store)

    handler.do_GET()

    assert ("status", 302) in sent
    location = urlsplit(dict(sent)["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://slack.com/oauth/v2/authorize"
    query = parse_qs(location.query)
    assert query["redirect_uri"] == [f"{BASE}/slack/oauth/callback"]
    assert query["client_id"] == ["slack-client"]

    monkeypatch.delenv("SLACK_CLIENT_SECRET")
    handler, sent, json_sent = _alias_handler("/slack/install/", store)

    handler.do_GET()

    assert sent == []
    assert json_sent == [(500, {"error": "slack OAuth is not configured"})]


def test_slack_callback_rejects_bad_state_before_exchange(tmp_path, slack_env) -> None:
    store = ConnectionStore(tmp_path / "installs.db")
    slack = _Slack({"ok": True, "access_token": "xoxb-secret", "team": {"id": "T1"}})
    _start(store)
    handler, sent, _ = _alias_handler(
        "/slack/oauth/callback?state=bad-state&code=oauth-code", store, slack.transport)

    handler.do_GET()

    assert slack.seen == []
    assert store.list() == []
    assert dict(sent)["Location"] == "/?slack=error#settings"


def test_slack_callback_upserts_install_and_redirects(tmp_path, slack_env) -> None:
    store = ConnectionStore(tmp_path / "installs.db")
    slack = _Slack({
        "ok": True,
        "access_token": "xoxb-secret",
        "team": {"id": "T1", "name": "Acme"},
        "scope": "chat:write",
    })
    state = _start(store)
    handler, sent, _ = _alias_handler(
        f"/slack/oauth/callback?state={state}&code=oauth-code", store, slack.transport)

    handler.do_GET()

    (exchange,) = slack.seen
    form = parse_qs(exchange.content.decode())
    assert form["code"] == ["oauth-code"]
    assert form["redirect_uri"] == [f"{BASE}/slack/oauth/callback"]
    legacy = SlackInstallStore(store.path)
    assert legacy.get("T1") == {
        "team_id": "T1", "team_name": "Acme", "scopes": "chat:write",
        "installed_at": legacy.get("T1")["installed_at"],
        "updated_at": legacy.get("T1")["updated_at"],
    }
    assert legacy.get_token("T1") == "xoxb-secret"
    assert dict(sent)["Location"] == "/?slack=connected#settings"


def test_slack_callback_rejection_does_not_log_secrets(
    tmp_path, slack_env, caplog: pytest.LogCaptureFixture,
) -> None:
    token = "xoxb-token-never-log"
    code = "oauth-code-never-log"
    store = ConnectionStore(tmp_path / "installs.db")
    slack = _Slack({"ok": False, "error": "invalid_code", "access_token": token})
    state = _start(store)
    handler, sent, _ = _alias_handler(
        f"/slack/oauth/callback?state={state}&code={code}", store, slack.transport)
    caplog.set_level(logging.WARNING)

    handler.do_GET()

    assert "invalid_code" in caplog.text
    assert "client-secret-never-log" not in caplog.text
    assert token not in caplog.text
    assert code not in caplog.text
    assert store.list() == []
    assert dict(sent)["Location"] == "/?slack=error#settings"


def test_slack_installs_response_reprojects_token_free_fields() -> None:
    store = _InstallStore([{
        "team_id": "T1",
        "team_name": "Acme",
        "scopes": "chat:write",
        "installed_at": 1000.0,
        "updated_at": 2000.0,
        "bot_token": "xoxb-must-not-escape",
    }])
    handler = _handler("/slack/installs", store)
    responses: list[tuple[int, dict]] = []
    handler._send_json = lambda code, payload: responses.append((code, payload))

    handler.do_GET()

    assert responses == [(200, {"installs": [{
        "team_id": "T1",
        "team_name": "Acme",
        "installed_at": 1000.0,
    }]})]
    install = responses[0][1]["installs"][0]
    assert "bot_token" not in install
    assert "scopes" not in install


def test_slack_uninstall_deletes_team_and_returns_result() -> None:
    store = _InstallStore(delete_result=True)
    path = "/slack/installs/T1.prod/delete?source=settings"
    assert _SLACK_UNINSTALL_RE.match("/slack/installs/T1.prod/delete")
    handler = _handler(path, store)
    responses: list[tuple[int, dict]] = []
    handler._send_json = lambda code, payload: responses.append((code, payload))

    handler.do_POST()

    assert store.delete_calls == ["T1.prod"]
    assert responses == [(200, {"deleted": True})]
