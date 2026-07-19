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


def _clear_thread_state() -> None:
    slack_events._thread_by_session.clear()
    slack_events._session_by_thread.clear()
    slack_events._pending.clear()


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


def test_route_question_posts_to_registered_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_thread_state()
    calls: list[tuple[object, ...]] = []
    slack_events.register_thread("session-1", "C123", "100.01")

    def fake_post(cfg: dict, text: str, channel: str, thread_ts: str | None = None) -> bool:
        calls.append((cfg, text, channel, thread_ts))
        return True

    monkeypatch.setattr(slack_events, "post_slack_message", fake_post)

    assert slack_events.route_question_to_thread(
        {"enabled": True},
        "session-1",
        "summarize this",
        "question-1",
        "Which format?",
        ["Brief", "Detailed"],
    )
    assert calls[0][2:] == ("C123", "100.01")
    assert "Reply with one of: Brief / Detailed" in calls[0][1]
    assert slack_events._pending["session-1"] == {
        "question_id": "question-1",
        "options": ["Brief", "Detailed"],
    }


def test_route_question_opens_owned_channel_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_thread_state()
    calls: list[tuple[object, ...]] = []

    def fake_open(cfg: dict, channel: str, text: str) -> str | None:
        calls.append(("open", cfg, channel, text))
        return "200.02"

    def fake_post(cfg: dict, text: str, channel: str, thread_ts: str | None = None) -> bool:
        calls.append(("post", cfg, text, channel, thread_ts))
        return True

    monkeypatch.setattr(slack_events, "open_slack_thread", fake_open)
    monkeypatch.setattr(slack_events, "post_slack_message", fake_post)

    assert slack_events.route_question_to_thread(
        {"enabled": True, "owned_channel": "COWNED"},
        "session-2",
        "write a release note",
        "question-2",
        "Who is the audience?",
    )
    assert calls[0] == (
        "open",
        {"enabled": True, "owned_channel": "COWNED"},
        "COWNED",
        "New run: write a release note",
    )
    assert calls[1][3:] == ("COWNED", "200.02")
    assert slack_events._thread_by_session["session-2"] == ("COWNED", "200.02")
    assert slack_events._session_by_thread[("COWNED", "200.02")] == "session-2"


def test_thread_reply_answers_pending_question_and_ignores_other_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_thread_state()
    acknowledgements: list[tuple[str, str, str | None]] = []
    slack_events.register_thread("session-3", "C123", "300.03")
    slack_events._pending["session-3"] = {
        "question_id": "question-3",
        "options": ["Brief", "Detailed"],
    }

    class Service:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, str | None]] = []

        def answer(
            self,
            session_id: str,
            question_id: str,
            answer: str,
            selected_option: str | None = None,
        ) -> None:
            self.calls.append((session_id, question_id, answer, selected_option))

    def fake_post(cfg: dict, text: str, channel: str, thread_ts: str | None = None) -> bool:
        acknowledgements.append((text, channel, thread_ts))
        return True

    monkeypatch.setattr(slack_events, "post_slack_message", fake_post)
    service = Service()
    slack_events.handle_message_event(
        {"channel": "C123", "thread_ts": "300.03", "text": "detailed"}, service
    )
    assert service.calls == [("session-3", "question-3", "detailed", "Detailed")]
    assert "session-3" not in slack_events._pending
    assert acknowledgements == [("Got it.", "C123", "300.03")]

    slack_events.handle_message_event(
        {
            "channel": "C123",
            "thread_ts": "300.03",
            "text": "ignored",
            "bot_id": "B1",
        },
        service,
    )
    slack_events.handle_message_event({"channel": "C123", "text": "ignored"}, service)
    slack_events.handle_message_event(
        {"channel": "C999", "thread_ts": "999.99", "text": "ignored"}, service
    )
    assert service.calls == [("session-3", "question-3", "detailed", "Detailed")]
