from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fabri.service import slack_oauth
from fabri.service.http_server import _Handler, _SLACK_UNINSTALL_RE

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


def test_slack_install_redirect_and_unconfigured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_url = "https://slack.com/oauth/v2/authorize?state=signed-state"
    monkeypatch.setattr(slack_oauth, "build_install_redirect", lambda: install_url)
    handler = _handler("/slack/install")
    responses: list[int] = []
    headers: list[tuple[str, str]] = []
    handler.send_response = responses.append
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: None

    handler.do_GET()

    assert responses == [302]
    assert ("Location", install_url) in headers

    monkeypatch.setattr(slack_oauth, "build_install_redirect", lambda: None)
    handler = _handler("/slack/install/")
    json_responses: list[tuple[int, dict]] = []
    handler._send_json = lambda code, payload: json_responses.append((code, payload))

    handler.do_GET()

    assert json_responses == [
        (500, {"error": "Slack install is not configured"})
    ]


def test_slack_callback_rejects_bad_state_before_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InstallStore()
    handler = _handler("/slack/oauth/callback", store)
    redirects: list[str] = []
    handler._redirect_studio = redirects.append
    verify_state = Mock(return_value=False)
    exchange_code = Mock()
    monkeypatch.setattr(slack_oauth, "verify_state", verify_state)
    monkeypatch.setattr(slack_oauth, "exchange_code", exchange_code)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing-secret")

    handler._slack_oauth_callback(
        {"state": ["bad-state"], "code": ["oauth-code"]}
    )

    verify_state.assert_called_once_with("bad-state", "signing-secret")
    exchange_code.assert_not_called()
    assert store.upsert_calls == []
    assert redirects == ["/?slack=error#settings"]


def test_slack_callback_upserts_install_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InstallStore()
    handler = _handler("/slack/oauth/callback", store)
    redirects: list[str] = []
    handler._redirect_studio = redirects.append
    verify_state = Mock(return_value=True)
    exchange_code = Mock(return_value={
        "ok": True,
        "access_token": "xoxb-secret",
        "team": {"id": "T1", "name": "Acme"},
        "scope": "chat:write",
    })
    monkeypatch.setattr(slack_oauth, "verify_state", verify_state)
    monkeypatch.setattr(slack_oauth, "exchange_code", exchange_code)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing-secret")

    handler._slack_oauth_callback(
        {"state": ["signed-state"], "code": ["oauth-code"]}
    )

    verify_state.assert_called_once_with("signed-state", "signing-secret")
    exchange_code.assert_called_once_with("oauth-code")
    assert store.upsert_calls == [{
        "team_id": "T1",
        "bot_token": "xoxb-secret",
        "team_name": "Acme",
        "scopes": "chat:write",
    }]
    assert redirects == ["/?slack=connected#settings"]


def test_slack_callback_rejection_does_not_log_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    signing_secret = "signing-secret-never-log"
    token = "xoxb-token-never-log"
    code = "oauth-code-never-log"
    handler = _handler("/slack/oauth/callback")
    redirects: list[str] = []
    handler._redirect_studio = redirects.append
    monkeypatch.setenv("SLACK_SIGNING_SECRET", signing_secret)
    monkeypatch.setattr(slack_oauth, "verify_state", lambda _state, _secret: True)
    monkeypatch.setattr(
        slack_oauth,
        "exchange_code",
        lambda _code: {
            "ok": False,
            "error": "invalid_code",
            "access_token": token,
        },
    )
    caplog.set_level(logging.WARNING, logger="fabri")

    handler._slack_oauth_callback(
        {"state": ["signed-state"], "code": [code]}
    )

    assert "invalid_code" in caplog.text
    assert signing_secret not in caplog.text
    assert token not in caplog.text
    assert code not in caplog.text
    assert redirects == ["/?slack=error#settings"]


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
