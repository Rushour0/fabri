"""Tenant-aware Slack Events API tests."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from fabri.service import slack_events


class _InstallStore:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def get_token(self, team_id: str) -> str | None:
        self.get_calls.append(team_id)
        return self.token

    def delete(self, team_id: str) -> bool:
        self.delete_calls.append(team_id)
        return True


class _Service:
    def __init__(self, install_store: _InstallStore) -> None:
        self.install_store = install_store


def _signed_request(payload: dict, secret: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    signature = "v0=" + hmac.new(
        secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return raw_body, {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }


def test_stored_team_token_is_passed_to_message_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tenant-signing-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    install_store = _InstallStore("xoxb-team-token")
    service = _Service(install_store)
    base_cfg = {"enabled": False, "signing_secret_env": "SLACK_SIGNING_SECRET"}
    captured_cfgs: list[dict] = []

    def capture_message(event: dict, service_arg: object, cfg: dict) -> None:
        captured_cfgs.append(cfg)

    monkeypatch.setattr(slack_events, "handle_message_event", capture_message)
    raw_body, headers = _signed_request(
        {
            "event_id": "Ev-tenant-stored",
            "team_id": "T-STORED",
            "event": {"type": "message"},
        },
        secret,
    )

    assert slack_events.handle_slack_event(
        raw_body, headers, service, base_cfg
    ) == (200, "", {})
    assert install_store.get_calls == ["T-STORED"]
    assert captured_cfgs[0]["bot_token"] == "xoxb-team-token"
    assert captured_cfgs[0]["enabled"] is True
    assert captured_cfgs[0] is not base_cfg


def test_team_without_install_uses_original_base_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "legacy-signing-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    install_store = _InstallStore()
    service = _Service(install_store)
    base_cfg = {
        "enabled": True,
        "bot_token_env": "SLACK_BOT_TOKEN",
        "signing_secret_env": "SLACK_SIGNING_SECRET",
    }
    captured_cfgs: list[dict] = []

    def capture_message(event: dict, service_arg: object, cfg: dict) -> None:
        captured_cfgs.append(cfg)

    monkeypatch.setattr(slack_events, "handle_message_event", capture_message)
    raw_body, headers = _signed_request(
        {
            "event_id": "Ev-tenant-legacy",
            "team_id": "T-LEGACY",
            "event": {"type": "message"},
        },
        secret,
    )

    assert slack_events.handle_slack_event(
        raw_body, headers, service, base_cfg
    ) == (200, "", {})
    assert install_store.get_calls == ["T-LEGACY"]
    assert captured_cfgs == [base_cfg]
    assert captured_cfgs[0] is base_cfg
    assert "bot_token" not in captured_cfgs[0]


@pytest.mark.parametrize("event_type", ["app_uninstalled", "tokens_revoked"])
def test_lifecycle_event_deletes_install(
    event_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "lifecycle-signing-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    install_store = _InstallStore("xoxb-unused-token")
    service = _Service(install_store)
    raw_body, headers = _signed_request(
        {
            "event_id": f"Ev-{event_type}",
            "team_id": "T-LIFECYCLE",
            "event": {"type": event_type},
        },
        secret,
    )

    assert slack_events.handle_slack_event(
        raw_body, headers, service, {}
    ) == (200, "", {})
    assert install_store.delete_calls == ["T-LIFECYCLE"]
    assert install_store.get_calls == []


def test_forged_request_never_touches_install_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "forgery-signing-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    install_store = _InstallStore("xoxb-unused-token")
    service = _Service(install_store)
    raw_body, headers = _signed_request(
        {
            "event_id": "Ev-forged-lifecycle",
            "team_id": "T-FORGED",
            "event": {"type": "app_uninstalled"},
        },
        secret,
    )
    headers["X-Slack-Signature"] = "v0=forged"

    assert slack_events.handle_slack_event(
        raw_body, headers, service, {}
    ) == (401, "invalid signature", {})
    assert install_store.delete_calls == []
    assert install_store.get_calls == []
