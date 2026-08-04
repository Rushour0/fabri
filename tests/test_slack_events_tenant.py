"""Tenant-aware Slack Events API tests."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from fabri.service import slack_events
from fabri.service.surfaces import pipeline
from fabri.service.surfaces import slack as slack_surface


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


def test_stored_team_token_is_used_when_replying_to_that_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply goes out on the *connecting* workspace's own bot token.

    The token is resolved when we actually post rather than for every inbound
    event, so this asserts the guarantee where it now lives: the delivery.
    """
    secret = "tenant-signing-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    pipeline.reset_state()
    install_store = _InstallStore("xoxb-team-token")
    service = _Service(install_store)
    base_cfg = {"enabled": False, "signing_secret_env": "SLACK_SIGNING_SECRET"}
    posted: list[dict] = []

    def fake_post(cfg: dict, text: str, channel: str, thread_ts: str | None = None) -> bool:
        posted.append(cfg)
        return True

    monkeypatch.setattr(slack_surface, "post_slack_message", fake_post)
    # A thread reply that answers this run's pending question.
    slack_events.register_thread("session-tenant", "C1", "1.0")
    pipeline.set_pending_question("session-tenant", {"question_id": "q1", "options": []})
    service.answer = lambda *a, **k: None  # type: ignore[attr-defined]

    raw_body, headers = _signed_request(
        {
            "event_id": "Ev-tenant-stored",
            "team_id": "T-STORED",
            "event": {
                "type": "message",
                "channel": "C1",
                "thread_ts": "1.0",
                "text": "go ahead",
            },
        },
        secret,
    )

    assert slack_events.handle_slack_event(
        raw_body, headers, service, base_cfg
    ) == (200, "", {})
    assert install_store.get_calls == ["T-STORED"]
    assert posted[0]["bot_token"] == "xoxb-team-token"
    assert posted[0]["enabled"] is True
    assert posted[0] is not base_cfg


def test_team_without_install_falls_back_to_the_server_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stored install: the single-tenant env token path is preserved."""
    secret = "legacy-signing-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    pipeline.reset_state()
    install_store = _InstallStore()
    service = _Service(install_store)
    base_cfg = {
        "enabled": True,
        "bot_token_env": "SLACK_BOT_TOKEN",
        "signing_secret_env": "SLACK_SIGNING_SECRET",
    }
    posted: list[dict] = []

    def fake_post(cfg: dict, text: str, channel: str, thread_ts: str | None = None) -> bool:
        posted.append(cfg)
        return True

    monkeypatch.setattr(slack_surface, "post_slack_message", fake_post)
    slack_events.register_thread("session-legacy", "C1", "1.0")
    pipeline.set_pending_question("session-legacy", {"question_id": "q1", "options": []})
    service.answer = lambda *a, **k: None  # type: ignore[attr-defined]

    raw_body, headers = _signed_request(
        {
            "event_id": "Ev-tenant-legacy",
            "team_id": "T-LEGACY",
            "event": {
                "type": "message",
                "channel": "C1",
                "thread_ts": "1.0",
                "text": "go ahead",
            },
        },
        secret,
    )

    assert slack_events.handle_slack_event(
        raw_body, headers, service, base_cfg
    ) == (200, "", {})
    assert install_store.get_calls == ["T-LEGACY"]
    assert posted == [base_cfg]
    assert "bot_token" not in posted[0]


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
