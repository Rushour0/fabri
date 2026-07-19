"""Unit tests for Slack Events API mention dispatching."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

import pytest

from fabri.service import slack_events
from fabri.service.slack_events import handle_slack_event, verify_slack_signature


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_verify_slack_signature_rejects_tampering_and_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    now = str(int(time.time()))
    body = b'{"event_id":"E1"}'
    signature = _signature("secret", now, body)
    assert verify_slack_signature("secret", now, body, signature)
    assert not verify_slack_signature("secret", now, b'{"event_id":"E2"}', signature)
    assert not verify_slack_signature("secret", str(int(time.time()) - 301), body, signature)


def test_url_verification_needs_no_signature() -> None:
    status, body, headers = handle_slack_event(
        b'{"type":"url_verification","challenge":"challenge-token"}', {}, object(), {}
    )
    assert (status, body, headers) == (200, "challenge-token", {"Content-Type": "text/plain"})


def test_app_mention_dispatches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "test-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    slack_events._event_ids.clear()
    dispatched = threading.Event()
    calls: list[object] = []

    class Service:
        def submit(self, task: str, *, catalog_ref: str | None = None) -> str:
            calls.append(("submit", task, catalog_ref))
            return "session-1"

        def result(self, session_id: str) -> dict:
            calls.append(("result", session_id))
            return {"final_text": "Done"}

    def fake_post(cfg: dict, text: str, channel: str, thread_ts: str | None = None) -> bool:
        calls.append(("post", text, channel, thread_ts))
        if text == "Done":
            dispatched.set()
        return True

    monkeypatch.setattr(slack_events, "post_slack_message", fake_post)
    payload = {
        "event_id": "Ev-1",
        "event": {
            "type": "app_mention",
            "channel": "C123",
            "ts": "123.45",
            "text": "<@U_BOT> summarize this",
        },
    }
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": _signature(secret, timestamp, raw),
    }
    cfg = {"enabled": True, "signing_secret_env": "SLACK_SIGNING_SECRET"}
    assert handle_slack_event(raw, headers, Service(), cfg) == (200, "", {})
    assert dispatched.wait(1)
    assert ("submit", "summarize this", None) in calls
    assert ("post", "On it...", "C123", "123.45") in calls
    assert ("post", "Done", "C123", "123.45") in calls
    assert handle_slack_event(raw, headers, Service(), cfg) == (200, "", {})
    time.sleep(0.02)
    assert calls.count(("submit", "summarize this", None)) == 1
