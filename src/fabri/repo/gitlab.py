"""Small stdlib-only GitLab REST v4 adapter."""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .base import push_branch_with_url, token_url


class GitLabError(RuntimeError):
    """A GitLab API request failed."""


def _http(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "PRIVATE-TOKEN": token, "Content-Type": "application/json", "User-Agent": "fabri",
    })
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raise GitLabError(f"GitLab API {error.code}: {error.read().decode('utf-8', errors='replace')[:300]}") from error
    except URLError as error:
        raise GitLabError(f"GitLab API request failed: {error.reason}") from error


def _project(repo: str) -> str:
    return quote(repo, safe="")


class GitLabProvider:
    def __init__(self, token: str) -> None:
        self.token = token

    def open_or_update_issue(self, repo: str, title: str, body: str, key: str, labels: list[str]) -> str:
        marker = f"<!-- fabri:repo:{key} -->"
        description = f"{body.rstrip()}\n\n{marker}"
        root = f"https://gitlab.com/api/v4/projects/{_project(repo)}/issues"
        _, issues = _http("GET", f"{root}?state=opened", self.token)
        for issue in issues:
            if marker in (issue.get("description") or ""):
                _, updated = _http("PUT", f"{root}/{issue['iid']}", self.token, {"title": title, "description": description, "labels": ",".join(labels)})
                return updated["web_url"]
        _, created = _http("POST", root, self.token, {"title": title, "description": description, "labels": ",".join(labels)})
        return created["web_url"]

    def open_or_update_pr(self, repo: str, title: str, body: str, key: str, head: str, base: str) -> str:
        marker = f"<!-- fabri:repo:{key} -->"
        description = f"{body.rstrip()}\n\n{marker}"
        root = f"https://gitlab.com/api/v4/projects/{_project(repo)}/merge_requests"
        _, merge_requests = _http("GET", f"{root}?state=opened", self.token)
        for merge_request in merge_requests:
            if marker in (merge_request.get("description") or ""):
                _, updated = _http("PUT", f"{root}/{merge_request['iid']}", self.token, {"title": title, "description": description, "source_branch": head, "target_branch": base})
                return updated["web_url"]
        _, created = _http("POST", root, self.token, {"title": title, "description": description, "source_branch": head, "target_branch": base, "draft": True})
        return created["web_url"]

    def push_branch(self, repo: str, base: str, new_branch: str, file_path: str, file_text: str, commit_msg: str) -> None:
        push_branch_with_url(token_url("oauth2", self.token, "gitlab.com", repo), base, new_branch, file_path, file_text, commit_msg)
