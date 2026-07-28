"""Tests for the builtin ``slack_post`` recipe."""
from __future__ import annotations

import io
import json
import logging
import urllib.request
from typing import Any

import pytest

from fabri.tools.recipes import slack_post


class _Response:
    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _invoke(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    stdin = io.StringIO(json.dumps(args))
    stdout = io.StringIO()
    monkeypatch.setattr(slack_post.sys, "stdin", stdin)
    monkeypatch.setattr(slack_post.sys, "stdout", stdout)
    exit_code = slack_post.main()
    return exit_code, json.loads(stdout.getvalue())


def test_happy_path_posts_with_slack_notify_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    token = "xoxb-happy-path-secret"
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", token)
    monkeypatch.setattr(slack_post, "validate_url", lambda url: url)

    def fake_urlopen(
        request: urllib.request.Request, timeout: int
    ) -> _Response:
        assert timeout == 5
        requests.append(request)
        return _Response({"ok": True, "ts": "171234.5678", "channel": "C123"})

    monkeypatch.setattr(slack_post._opener, "open", fake_urlopen)

    exit_code, result = _invoke(
        monkeypatch, {"channel": "C123", "text": "Deploy complete"}
    )

    assert exit_code == 0
    assert result == {
        "ok": True,
        "result": {"ts": "171234.5678", "channel": "C123"},
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://slack.com/api/chat.postMessage"
    assert request.get_header("Authorization") == f"Bearer {token}"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8")) == {
        "channel": "C123",
        "text": "Deploy complete",
    }


def test_threaded_reply_includes_thread_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict[str, str]] = []
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", "xoxb-thread-secret")
    monkeypatch.setattr(slack_post, "validate_url", lambda url: url)

    def fake_urlopen(
        request: urllib.request.Request, timeout: int
    ) -> _Response:
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _Response({"ok": True, "ts": "171234.9999", "channel": "C456"})

    monkeypatch.setattr(slack_post._opener, "open", fake_urlopen)

    exit_code, result = _invoke(
        monkeypatch,
        {
            "channel": "C456",
            "text": "Thread update",
            "thread_ts": "171234.0001",
        }
    )

    assert exit_code == 0
    assert result["ok"] is True
    assert bodies == [
        {
            "channel": "C456",
            "text": "Thread update",
            "thread_ts": "171234.0001",
        }
    ]


def test_missing_default_credential_names_typed_env_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRI_CRED_SLACK_DEFAULT", raising=False)
    monkeypatch.setattr(slack_post, "validate_url", lambda url: url)

    def fail_urlopen(*_: object, **__: object) -> _Response:
        pytest.fail("Slack must not be called without a resolved credential")

    monkeypatch.setattr(slack_post._opener, "open", fail_urlopen)

    exit_code, result = _invoke(
        monkeypatch, {"channel": "C123", "text": "Hello"}
    )

    assert exit_code == 1
    assert result["ok"] is False
    assert result["error"].startswith("CredentialNotFoundError:")
    assert "FABRI_CRED_SLACK_DEFAULT" in result["error"]
    assert "xoxb-" not in result["error"]


def test_slack_side_error_is_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", "xoxb-api-error-secret")
    monkeypatch.setattr(slack_post, "validate_url", lambda url: url)
    monkeypatch.setattr(
        slack_post._opener,
        "open",
        lambda *_args, **_kwargs: _Response(
            {"ok": False, "error": "channel_not_found"}
        ),
    )

    exit_code, result = _invoke(
        monkeypatch, {"channel": "C404", "text": "Hello"}
    )

    assert exit_code == 1
    assert result == {"ok": False, "error": "channel_not_found"}


def test_token_never_leaks_through_errors_logs_or_repr(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "xoxb-never-leak-this-token"
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", token)
    monkeypatch.setattr(slack_post, "validate_url", lambda url: url)
    caplog.set_level(logging.DEBUG)

    def fail_with_request_details(
        request: urllib.request.Request, timeout: int
    ) -> _Response:
        raise OSError(f"request failed: {request.headers!r}")

    monkeypatch.setattr(
        slack_post._opener, "open", fail_with_request_details
    )

    exit_code, result = _invoke(
        monkeypatch, {"channel": "C123", "text": "Hello"}
    )

    assert exit_code == 1
    assert result == {"ok": False, "error": "Slack request failed (OSError)"}
    assert token not in result["error"]
    assert token not in repr(result)
    assert token not in caplog.text
