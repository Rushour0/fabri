from __future__ import annotations

import base64
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fabri import cli
from fabri.repo import jira_poll
from fabri.repo.jira_issue import JiraClient, load_routes, scoped_comment_approver
from fabri.repo.jira_poll import PollState, poll_once

pytestmark = pytest.mark.integration


class _JiraHandler(BaseHTTPRequestHandler):
    comments: list[dict] = []
    auth_headers: list[str] = []
    jqls: list[str] = []
    comment_paths: list[str] = []

    issues = {
        "AI-1": {
            "key": "AI-1",
            "fields": {
                "summary": "Eligible",
                "labels": ["fabri:fix"],
                "assignee": {"accountId": "owner-1"},
                "creator": {"accountId": "creator-1"},
                "created": "2026-01-01T00:00:00.000+0000",
            },
        },
        "AI-2": {
            "key": "AI-2",
            "fields": {
                "summary": "No label",
                "labels": [],
                "assignee": {"accountId": "owner-2"},
                "creator": {"accountId": "creator-2"},
                "created": "2026-01-01T00:00:00.000+0000",
            },
        },
        "AI-3": {
            "key": "AI-3",
            "fields": {
                "summary": "Wrong label author",
                "labels": ["fabri:fix"],
                "assignee": {"accountId": "owner-3"},
                "creator": {"accountId": "creator-3"},
                "created": "2026-01-01T00:00:00.000+0000",
            },
        },
    }
    histories = {
        "AI-1": [
            {
                "id": "101",
                "created": "2026-01-02T00:00:00.000+0000",
                "author": {"accountId": "owner-1"},
                "items": [{"field": "labels", "fromString": "", "toString": "fabri:fix"}],
            }
        ],
        "AI-2": [],
        "AI-3": [
            {
                "id": "103",
                "created": "2026-01-02T00:00:00.000+0000",
                "author": {"accountId": "someone-else"},
                "items": [{"field": "labels", "fromString": "", "toString": "fabri:fix"}],
            }
        ],
    }

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.auth_headers.append(self.headers.get("Authorization", ""))
        split = urlsplit(self.path)
        path = split.path
        if path == "/rest/api/3/search/jql":
            jql = parse_qs(split.query).get("jql", [""])[0]
            self.jqls.append(jql)
            matches = [
                issue for issue in self.issues.values()
                if 'labels = "fabri:fix"' in jql and "fabri:fix" in issue["fields"]["labels"]
            ]
            self._json(200, {"issues": matches, "isLast": True})
            return
        parts = path.strip("/").split("/")
        if len(parts) == 6 and parts[:4] == ["rest", "api", "3", "issue"]:
            key = parts[4]
            if parts[5] == "changelog":
                values = self.histories[key]
                self._json(
                    200,
                    {"startAt": 0, "maxResults": 100, "total": len(values), "values": values},
                )
                return
        if len(parts) == 5 and parts[:4] == ["rest", "api", "3", "issue"]:
            self._json(200, self.issues[parts[4]])
            return
        self._json(404, {"error": path})

    def do_POST(self) -> None:
        self.auth_headers.append(self.headers.get("Authorization", ""))
        self.comment_paths.append(urlsplit(self.path).path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.comments.append(body)
        self._json(201, {"id": str(len(self.comments))})


class _RewriteTransport(httpx.BaseTransport):
    def __init__(self, port: int) -> None:
        self.port = port
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        rewritten = httpx.Request(
            request.method,
            url,
            headers=request.headers,
            content=request.read(),
        )
        return self.inner.handle_request(rewritten)

    def close(self) -> None:
        self.inner.close()


@pytest.fixture
def jira_server():
    for recorded in (_JiraHandler.comments, _JiraHandler.auth_headers,
                     _JiraHandler.jqls, _JiraHandler.comment_paths):
        recorded.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JiraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_routes(tmp_path: Path) -> Path:
    crew = tmp_path / "crew"
    crew.mkdir()
    path = tmp_path / "routes.toml"
    path.write_text(
        """
label = "fabri:fix"
max_cost_usd = 10.0
agency = "crew"
[projects.AI]
repo = "owner/repo"
base = "main"
test_cmd = "pytest"
""",
        encoding="utf-8",
    )
    return path


def test_cli_poll_local_jira_claims_once_and_skips_non_assignee(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    jira_server: ThreadingHTTPServer,
) -> None:
    config_path = _write_routes(tmp_path)
    monkeypatch.setenv("FABRI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JIRA_URL", "https://callplaybook.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")

    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host in {"127.0.0.1", b"127.0.0.1"}:
            return original_getaddrinfo(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)
    calls: list[str] = []
    transport = _RewriteTransport(jira_server.server_port)

    def dependencies(routes):
        client = JiraClient("https://callplaybook.atlassian.net", transport=transport)
        state = PollState(tmp_path / "home" / "jira" / "poll.db")

        def runner(**kwargs):
            calls.append(kwargs["issue_key"])
            return {"ok": True, "pr_url": "https://github.test/pr/1"}

        return {"client": client, "state": state, "runner": runner, "sleep": lambda seconds: None}

    monkeypatch.setattr(jira_poll, "_poll_dependencies", dependencies)

    for _ in range(2):
        monkeypatch.setattr(sys, "argv", ["fabri", "jira", "poll", "--config", str(config_path)])
        cli.main()

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert calls == ["AI-1"]
    assert any(record["key"] == "AI-3" and "assignee" in record["reason"] for record in records)
    assert any(record["key"] == "AI-1" and record["action"] == "seen" for record in records)
    assert not any(record["key"] == "AI-2" for record in records)
    assert _JiraHandler.jqls[0] == (
        'project = "AI" AND labels = "fabri:fix" AND statusCategory != Done ORDER BY created ASC'
    )
    expected = "Basic " + base64.b64encode(b"bot@example.com:token").decode()
    assert _JiraHandler.auth_headers and set(_JiraHandler.auth_headers) == {expected}
    assert _JiraHandler.comments == []

    # The scoped approver lets exactly one comment reach Jira, on that issue only.
    approver = scoped_comment_approver("AI-1")
    doc = {"type": "doc", "version": 1, "content": []}
    refused = client_for_comments(transport).add_comment("AI-2", doc, approver)
    assert refused.get("ok") is not True
    assert _JiraHandler.comment_paths == []
    sent = client_for_comments(transport).add_comment("AI-1", doc, approver)
    assert sent.get("ok") is True
    again = client_for_comments(transport).add_comment("AI-1", doc, approver)
    assert again.get("ok") is not True
    assert _JiraHandler.comment_paths == ["/rest/api/3/issue/AI-1/comment"]
    transport.close()


def client_for_comments(transport: httpx.BaseTransport) -> JiraClient:
    return JiraClient("https://callplaybook.atlassian.net", transport=transport)


def test_poller_skips_issue_returned_without_label(tmp_path: Path) -> None:
    routes = load_routes(_write_routes(tmp_path))
    issue = _JiraHandler.issues["AI-2"]

    class Client:
        def search(self, jql: str, fields: list[str]) -> list[dict]:
            del jql, fields
            return [issue]

        def get_changelog(self, key: str) -> list[dict]:
            del key
            return []

    runner = MockRunner()
    records = poll_once(
        routes,
        client=Client(),
        state=PollState(tmp_path / "poll.db"),
        runner=runner,
    )
    assert records[0]["action"] == "skipped"
    assert "not on the issue" in records[0]["reason"]
    assert runner.calls == 0


class MockRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.calls += 1
        return {"ok": True}
