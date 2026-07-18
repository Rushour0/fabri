"""Unit tests for outbound Slack ask_user notifications."""
from __future__ import annotations

import json
import urllib.request

import pytest

from fabri.service.slack_notify import post_ask_user_question


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_disabled_config_does_not_post(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_urlopen(*_: object, **__: object) -> _Response:
        nonlocal called
        called = True
        return _Response({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert not post_ask_user_question(
        {"enabled": False}, session_id="session-1", question="Continue?", question_id="q-1"
    )
    assert not called


def test_missing_token_skips_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_urlopen(*_: object, **__: object) -> _Response:
        pytest.fail("urlopen must not be called without a token")

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    assert not post_ask_user_question(
        {"enabled": True, "bot_token_env": "SLACK_BOT_TOKEN", "default_channel": "#questions"},
        session_id="session-1",
        question="Continue?",
        question_id="q-1",
    )
    assert "token environment variable SLACK_BOT_TOKEN is unset" in caplog.text


def test_posts_question_to_default_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> _Response:
        assert timeout == 5
        requests.append(request)
        return _Response({"ok": True})

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert post_ask_user_question(
        {"enabled": True, "bot_token_env": "SLACK_BOT_TOKEN", "default_channel": "#questions"},
        session_id="session-1",
        question="Continue?",
        question_id="q-1",
        options=["yes", "no"],
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://slack.com/api/chat.postMessage"
    assert request.get_header("Authorization") == "Bearer xoxb-secret"
    body = json.loads(request.data.decode("utf-8"))
    assert body["channel"] == "#questions"
    assert "Continue?" in body["text"]
    assert "session-1" in body["text"]


def test_posts_to_default_user_without_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> _Response:
        requests.append(request)
        return _Response({"ok": True})

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert post_ask_user_question(
        {"enabled": True, "bot_token_env": "SLACK_BOT_TOKEN", "default_user": "U0123456"},
        session_id="session-1",
        question="Continue?",
        question_id="q-1",
    )
    body = json.loads(requests[0].data.decode("utf-8"))
    assert body["channel"] == "U0123456"


def test_network_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(*_: object, **__: object) -> _Response:
        raise OSError("network unavailable")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setattr(urllib.request, "urlopen", raise_error)

    assert not post_ask_user_question(
        {"enabled": True, "bot_token_env": "SLACK_BOT_TOKEN", "default_channel": "#questions"},
        session_id="session-1",
        question="Continue?",
        question_id="q-1",
    )
