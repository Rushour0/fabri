"""Offline tests for the stdlib GitHub Issues client."""
from __future__ import annotations

import io
from urllib.error import HTTPError

import pytest

from fabri.repo import github, open_or_update_tracking_issue


def test_create_and_comment_issue_post_expected_request(monkeypatch):
    calls: list[tuple[str, str, str, dict | None]] = []

    def fake_http(method, url, token, payload=None, timeout=30):
        calls.append((method, url, token, payload))
        if url.endswith("/comments"):
            return 201, {"html_url": "https://github.com/acme/widget/issues/7#comment"}
        return 201, {"html_url": "https://github.com/acme/widget/issues/7"}

    monkeypatch.setattr(github, "_http", fake_http)

    assert github.create_issue("acme/widget", "secret", "Title", "Body", ["fabri"]) == (
        "https://github.com/acme/widget/issues/7"
    )
    assert github.comment_issue("acme/widget", "secret", 7, "Update") == (
        "https://github.com/acme/widget/issues/7#comment"
    )
    assert calls == [
        (
            "POST",
            "https://api.github.com/repos/acme/widget/issues",
            "secret",
            {"title": "Title", "body": "Body", "labels": ["fabri"]},
        ),
        (
            "POST",
            "https://api.github.com/repos/acme/widget/issues/7/comments",
            "secret",
            {"body": "Update"},
        ),
    ]


def test_list_open_issues_with_label_excludes_pull_requests(monkeypatch):
    def fake_http(method, url, token, payload=None, timeout=30):
        assert method == "GET"
        assert "state=open&labels=fabri&sort=created&direction=desc" in url
        return 200, [
            {"number": 1, "body": "issue"},
            {"number": 2, "body": "pr", "pull_request": {}},
        ]

    monkeypatch.setattr(github, "_http", fake_http)
    assert github.list_open_issues_with_label("acme/widget", "secret", "fabri") == [
        {"number": 1, "body": "issue"}
    ]


def test_find_open_issue_with_marker_returns_match_or_none(monkeypatch):
    marker = "<!-- fabri:repo:daily -->"

    def fake_http(method, url, token, payload=None, timeout=30):
        return 200, [
            {"number": 1, "body": "unrelated"},
            {"number": 2, "body": f"tracking\n{marker}"},
        ]

    monkeypatch.setattr(github, "_http", fake_http)
    assert github.find_open_issue_with_marker("acme/widget", "secret", marker) == {
        "number": 2,
        "body": f"tracking\n{marker}",
    }
    assert github.find_open_issue_with_marker("acme/widget", "secret", "missing") is None


def test_open_or_update_tracking_issue_comments_when_marker_exists(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method, url, token, payload=None, timeout=30):
        calls.append((method, url, payload))
        if method == "GET":
            return 200, [{"number": 7, "html_url": "https://github.com/acme/widget/issues/7",
                          "body": "<!-- fabri:repo:daily -->"}]
        return 201, {"html_url": "https://github.com/acme/widget/issues/7#comment"}

    monkeypatch.setattr(github, "_http", fake_http)
    assert open_or_update_tracking_issue(
        "acme/widget", "secret", "Daily", "new status", "daily", ["fabri"]
    ) == "https://github.com/acme/widget/issues/7"
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][1].endswith("/issues/7/comments")
    assert "<!-- fabri:repo:daily -->" in calls[1][2]["body"]


def test_open_or_update_tracking_issue_creates_when_no_marker_exists(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method, url, token, payload=None, timeout=30):
        calls.append((method, url, payload))
        if method == "GET":
            return 200, []
        return 201, {"html_url": "https://github.com/acme/widget/issues/8"}

    monkeypatch.setattr(github, "_http", fake_http)
    assert open_or_update_tracking_issue(
        "acme/widget", "secret", "Daily", "new status", "daily", ["fabri"]
    ) == "https://github.com/acme/widget/issues/8"
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][1].endswith("/issues")
    assert calls[1][2]["labels"] == ["fabri"]


def test_http_error_includes_truncated_github_body(monkeypatch):
    body = b"x" * 400

    def fake_urlopen(request, timeout=30):
        raise HTTPError(request.full_url, 422, "unprocessable", {}, io.BytesIO(body))

    monkeypatch.setattr(github, "urlopen", fake_urlopen)
    with pytest.raises(github.GitHubError) as error:
        github._http("POST", "https://api.github.com/repos/acme/widget/issues", "secret", {})
    assert str(error.value) == f"GitHub API 422: {'x' * 300}"
