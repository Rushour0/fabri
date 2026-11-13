"""The Slack connector expansion and HTTP behavior, using recorded mock responses."""
from __future__ import annotations

import json
import socket
from collections.abc import Mapping

import httpx
import pytest

from fabri.tools.connector_catalog import slack
from fabri.tools.connectors import connector_entry
from fabri.tools.sources import SourceTool, build_source_tools

TOKEN = "xoxb-slack-test-token"
pytestmark = pytest.mark.unit


class Upstream:
    def __init__(self, response: object):
        self.response = response
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.response)


def _public_dns(
    _host: str,
    _port: int | str,
    *_args: object,
    **_kwargs: object,
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _tools(monkeypatch: pytest.MonkeyPatch, response: object) -> tuple[dict[str, SourceTool], Upstream]:
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", _public_dns)
    upstream = Upstream(response)
    tools: dict[str, SourceTool] = {}
    for declaration in connector_entry("slack")["sources"]:
        (tool,) = build_source_tools(declaration, transport=upstream.transport)
        tools[tool.manifest.name] = tool
    return tools, upstream


@pytest.mark.parametrize(
    "tool_name,args,path,expected_query,expected_body",
    [
        pytest.param(
            "slack_thread_replies",
            {"channel": "C1", "ts": "171.25", "limit": 50, "cursor": "next"},
            "/api/conversations.replies",
            {"channel": "C1", "ts": "171.25", "limit": "50", "cursor": "next"},
            None,
            id="thread-replies",
        ),
        pytest.param(
            "slack_permalink",
            {"channel": "C1", "message_ts": "171.25"},
            "/api/chat.getPermalink",
            {"channel": "C1", "message_ts": "171.25"},
            None,
            id="permalink",
        ),
        pytest.param(
            "slack_user_info",
            {"user": "U0123456"},
            "/api/users.info",
            {"user": "U0123456"},
            None,
            id="user-info",
        ),
        pytest.param(
            "slack_user_by_email",
            {"email": "user@example.test"},
            "/api/users.lookupByEmail",
            {"email": "user@example.test"},
            None,
            id="user-by-email",
        ),
        pytest.param(
            "slack_update_message",
            {"channel": "C1", "ts": "171.25", "text": "edited"},
            "/api/chat.update",
            {},
            {"channel": "C1", "ts": "171.25", "text": "edited", "unfurl_links": False,
             "unfurl_media": False},
            id="update-message",
        ),
    ],
)
def test_new_tool_request_shape(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: dict[str, object],
    path: str,
    expected_query: dict[str, str],
    expected_body: dict[str, object] | None,
) -> None:
    tools, upstream = _tools(monkeypatch, {"ok": True})

    result = tools[tool_name].handler(args)

    assert result["ok"] is True
    request = upstream.requests[-1]
    assert request.method == ("POST" if expected_body is not None else "GET")
    assert request.url.path == path
    assert dict(request.url.params) == expected_query
    if expected_body is not None:
        assert json.loads(request.content) == expected_body


def test_post_message_sends_blocks_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    tools, upstream = _tools(monkeypatch, {"ok": True})
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}}]

    result = tools["slack_post_message"].handler({"channel": "C1", "text": "fallback", "blocks": blocks})

    assert result["ok"] is True
    assert json.loads(upstream.requests[-1].content) == {
        "channel": "C1",
        "text": "fallback",
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def test_tool_approval_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _ = _tools(monkeypatch, {"ok": True})

    for name in ("slack_channels_list", "slack_channel_history", "slack_thread_replies", "slack_permalink",
                 "slack_user_info", "slack_user_by_email"):
        assert tools[name].manifest.approval == "read", name
    for name in ("slack_post_message", "slack_update_message"):
        assert tools[name].manifest.approval == "always_approve", name


@pytest.mark.parametrize(
    "tool_name,args",
    [
        pytest.param("slack_post_message", {"channel": "C1", "text": "hello"}, id="post-message"),
        pytest.param("slack_update_message", {"channel": "C1", "ts": "171.25", "text": "hello"},
                     id="update-message"),
    ],
)
def test_outbound_posts_drop_mass_mentions_and_disable_unfurls(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: dict[str, object],
) -> None:
    tools, upstream = _tools(monkeypatch, {"ok": True})
    unsafe = {**args, "link_names": 1, "parse": "full", "unfurl_links": True}

    result = tools[tool_name].handler(unsafe)

    assert result["ok"] is True
    body = json.loads(upstream.requests[-1].content)
    assert "link_names" not in body and "parse" not in body
    assert body["unfurl_links"] is False and body["unfurl_media"] is False


@pytest.mark.parametrize(
    "tool_name,args",
    [
        pytest.param("slack_channel_history", {"channel": "C1"}, id="read"),
        pytest.param("slack_update_message", {"channel": "C1", "ts": "171.25"}, id="write"),
    ],
)
def test_slack_http_200_error_becomes_tool_error(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: dict[str, object],
) -> None:
    body = {"ok": False, "error": "channel_not_found"}
    tools, _ = _tools(monkeypatch, body)

    result = tools[tool_name].handler(args)

    assert result["ok"] is False
    assert result["error"] == "slack error (channel_not_found)"
    assert result["result"] == {"status": "error", "code": 200, "data": body}


def test_slack_error_code_is_sanitised(monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _ = _tools(monkeypatch, {"ok": False, "error": "Bad Error!<script>"})

    result = tools["slack_user_info"].handler({"user": "U1"})

    assert result["ok"] is False
    assert result["error"] == "slack error (baderrorscript)"


def test_slack_success_keeps_response_data(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"ok": True, "user": {"id": "U1", "name": "Ada"}}
    tools, _ = _tools(monkeypatch, body)

    result = tools["slack_user_info"].handler({"user": "U1"})

    assert result["ok"] is True
    assert result["result"]["data"] == body


def test_shape_body_returns_a_new_mapping_without_mutating_input() -> None:
    original: Mapping[str, object] = {"text": "hello", "link_names": 1, "unfurl_links": True}

    shaped = slack.shape_body("/api/chat.postMessage", original)

    assert original == {"text": "hello", "link_names": 1, "unfurl_links": True}
    assert shaped == {"text": "hello", "unfurl_links": False, "unfurl_media": False}
    assert shaped is not original
