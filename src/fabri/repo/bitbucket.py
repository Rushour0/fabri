"""Small stdlib-only Bitbucket Cloud REST v2 adapter."""
from __future__ import annotations

import json
import base64
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import push_branch_with_url, token_url


class BitbucketError(RuntimeError):
    """A Bitbucket API request failed."""


def _http(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    authorization = f"Bearer {token}"
    if ":" in token:
        authorization = "Basic " + base64.b64encode(token.encode("utf-8")).decode("ascii")
    request = Request(url, data=data, method=method, headers={
        "Authorization": authorization, "Content-Type": "application/json", "User-Agent": "fabri",
    })
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raise BitbucketError(f"Bitbucket API {error.code}: {error.read().decode('utf-8', errors='replace')[:300]}") from error
    except URLError as error:
        raise BitbucketError(f"Bitbucket API request failed: {error.reason}") from error


class BitbucketProvider:
    def __init__(self, token: str) -> None:
        self.token = token

    def open_or_update_issue(self, repo: str, title: str, body: str, key: str, labels: list[str]) -> str:
        marker = f"<!-- fabri:repo:{key} -->"
        content = f"{body.rstrip()}\n\n{marker}"
        root = f"https://api.bitbucket.org/2.0/repositories/{repo}/issues"
        _, page = _http("GET", f"{root}?state=new", self.token)
        for issue in page.get("values", []):
            if marker in ((issue.get("content") or {}).get("raw") or ""):
                _, updated = _http("PUT", f"{root}/{issue['id']}", self.token, {"title": title, "content": {"raw": content}})
                return updated["links"]["html"]["href"]
        _, created = _http("POST", root, self.token, {"title": title, "content": {"raw": content}})
        return created["links"]["html"]["href"]

    def open_or_update_pr(self, repo: str, title: str, body: str, key: str, head: str, base: str) -> str:
        marker = f"<!-- fabri:repo:{key} -->"
        content = f"{body.rstrip()}\n\n{marker}"
        root = f"https://api.bitbucket.org/2.0/repositories/{repo}/pullrequests"
        _, page = _http("GET", f"{root}?state=OPEN", self.token)
        payload = {"title": title, "description": content, "source": {"branch": {"name": head}}, "destination": {"branch": {"name": base}}, "close_source_branch": False, "draft": True}
        for pull_request in page.get("values", []):
            if marker in (pull_request.get("description") or ""):
                _, updated = _http("PUT", f"{root}/{pull_request['id']}", self.token, payload)
                return updated["links"]["html"]["href"]
        _, created = _http("POST", root, self.token, payload)
        return created["links"]["html"]["href"]

    def push_branch(self, repo: str, base: str, new_branch: str, file_path: str, file_text: str, commit_msg: str) -> None:
        push_branch_with_url(token_url("x-token-auth", self.token, "bitbucket.org", repo), base, new_branch, file_path, file_text, commit_msg)
