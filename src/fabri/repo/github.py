"""Small, stdlib-only client for the GitHub Issues API."""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    """A GitHub API request failed."""


def _http(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """Make one GitHub API request and decode its JSON response."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fabri",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:300]
        raise GitHubError(f"GitHub API {error.code}: {body}") from error
    except URLError as error:
        raise GitHubError(f"GitHub API request failed: {error.reason}") from error


def _list_open_issues(
    repo: str, token: str, label: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    query: dict[str, str] = {"state": "open"}
    if label:
        query["labels"] = label
    query["sort"] = "created"
    query["direction"] = "desc"
    _, items = _http(
        "GET", f"https://api.github.com/repos/{repo}/issues?{urlencode(query)}", token
    )
    return [item for item in items if "pull_request" not in item][:limit]


def list_open_issues_with_label(
    repo: str, token: str, label: str, limit: int = 20
) -> list[dict[str, Any]]:
    """List recent open issues carrying *label*, excluding pull requests."""
    return _list_open_issues(repo, token, label=label, limit=limit)


def create_issue(
    repo: str,
    token: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str:
    """Create an issue and return its GitHub URL."""
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels is not None:
        payload["labels"] = labels
    _, issue = _http("POST", f"https://api.github.com/repos/{repo}/issues", token, payload)
    return issue["html_url"]


def comment_issue(repo: str, token: str, number: int, body: str) -> str:
    """Comment on an issue and return the comment URL."""
    _, comment = _http(
        "POST",
        f"https://api.github.com/repos/{repo}/issues/{number}/comments",
        token,
        {"body": body},
    )
    return comment["html_url"]


def find_open_issue_with_marker(
    repo: str,
    token: str,
    marker: str,
    label: str | None = None,
    limit: int = 30,
) -> dict[str, Any] | None:
    """Find a recent open issue whose body includes the stable fabri marker."""
    for issue in _list_open_issues(repo, token, label=label, limit=limit):
        if marker in (issue.get("body") or ""):
            return issue
    return None
