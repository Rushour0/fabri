"""Jira Cloud issue reads, searches, comments, links and transitions.

Creating an issue, adding a comment and changing a transition always need
approval. Every write is also floored by the Atlassian host.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from fabri.tools.connector_catalog import (
    Connector,
    ConnectorError,
    OAuthSpec,
    _credential,
)

ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

ENTRY = Connector(
    "jira",
    "Jira",
    "Jira Cloud: read and search issues, inspect comments, links and transitions, and make approved changes.",
    ("http",),
    token_env="JIRA_API_TOKEN",
    secret_provider="jira",
    needs_url=True,
    oauth=OAuthSpec(
        authorize_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes_default=("read:jira-work", "offline_access"),
        scopes_optional={"write": "write:jira-work"},
        client_id_env="JIRA_CLIENT_ID",
        secret_env="JIRA_CLIENT_SECRET",
        pkce=False,
        extra_authorize={"audience": "api.atlassian.com", "prompt": "consent"},
        identity_url=ACCESSIBLE_RESOURCES_URL,
        scope_separator=" ",
    ),
)

CHECK_URL = "https://example.atlassian.net"
HOSTS = ("api.atlassian.com", "atlassian.net")

_READ_METHODS = frozenset({"GET", "HEAD"})
_CLOUD_ID = r"[A-Za-z0-9][A-Za-z0-9-]{0,63}"
_ISSUE_KEY = r"(?:\{key\}|[A-Za-z][A-Za-z0-9_]*-[0-9]+|[0-9]+)"
_PREFIX = rf"(?:/ex/jira/{_CLOUD_ID})?"
READS = (
    ("GET", rf"{_PREFIX}/rest/api/3/issue/{_ISSUE_KEY}"),
    ("GET", rf"{_PREFIX}/rest/api/3/search/jql"),
    ("GET", rf"{_PREFIX}/rest/api/3/issue/{_ISSUE_KEY}/comment"),
    ("GET", rf"{_PREFIX}/rest/api/3/issue/{_ISSUE_KEY}/remotelink"),
    ("GET", rf"{_PREFIX}/rest/api/3/issue/{_ISSUE_KEY}/transitions"),
)

_ADF_SCHEMA = {
    "type": "object",
    "description": "Atlassian Document Format document",
}


def _api_base(url: str) -> tuple[str, bool]:
    """Return the normalized API base and whether it uses OAuth."""
    message = "jira url must be an Atlassian OAuth API base or a Jira Cloud site"
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ConnectorError(message) from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectorError(message)

    host = (parsed.hostname or "").lower()
    if host == "api.atlassian.com":
        match = re.fullmatch(rf"/ex/jira/({_CLOUD_ID})/?", parsed.path)
        if match is None:
            raise ConnectorError(message)
        return f"https://api.atlassian.com/ex/jira/{match.group(1)}", True

    match = re.fullmatch(r"([a-z0-9][a-z0-9-]{0,62})[.]atlassian[.]net", host)
    if match is None or parsed.path not in ("", "/"):
        raise ConnectorError(message)
    return f"https://{match.group(1).lower()}.atlassian.net", False


def _sources(name: str, base: str, auth: dict[str, Any]) -> list[dict[str, Any]]:
    issue_fields = {
        "type": "object",
        "properties": {
            "project": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "id": {"type": "string"}},
                "additionalProperties": False,
                "anyOf": [{"required": ["key"]}, {"required": ["id"]}],
            },
            "issuetype": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "id": {"type": "string"}},
                "additionalProperties": False,
                "anyOf": [{"required": ["name"]}, {"required": ["id"]}],
            },
            "summary": {"type": "string"},
            "description": _ADF_SCHEMA,
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["project", "issuetype", "summary"],
        "additionalProperties": True,
    }
    properties = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {}},
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    }
    return [
        {
            "kind": "http",
            "name": f"{name}_get_issue",
            "title": "Get Jira issue",
            "description": "Read one Jira issue by key or numeric id.",
            "url": f"{base}/rest/api/3/issue/{{key}}",
            "method": "GET",
            "headers": auth,
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "fields": {"type": "string", "description": "Comma-separated field names"},
                    "expand": {"type": "string"},
                },
                "required": ["key"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_search",
            "title": "Search Jira issues",
            "description": "Search Jira issues with JQL and return the requested fields.",
            "url": f"{base}/rest/api/3/search/jql",
            "method": "GET",
            "headers": auth,
            "input_schema": {
                "type": "object",
                "properties": {
                    "jql": {"type": "string"},
                    "fields": {"type": "string", "description": "Comma-separated field names"},
                    "maxResults": {"type": "integer", "maximum": 50},
                    "nextPageToken": {"type": "string"},
                },
                "required": ["jql", "fields"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_list_comments",
            "title": "List Jira comments",
            "description": "List comments on one Jira issue.",
            "url": f"{base}/rest/api/3/issue/{{key}}/comment",
            "method": "GET",
            "headers": auth,
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "startAt": {"type": "integer"},
                    "maxResults": {"type": "integer", "maximum": 100},
                    "orderBy": {"type": "string"},
                },
                "required": ["key"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_list_remote_links",
            "title": "List Jira remote links",
            "description": "List remote links attached to one Jira issue.",
            "url": f"{base}/rest/api/3/issue/{{key}}/remotelink",
            "method": "GET",
            "headers": auth,
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_list_transitions",
            "title": "List Jira transitions",
            "description": "List available workflow transitions for one Jira issue.",
            "url": f"{base}/rest/api/3/issue/{{key}}/transitions",
            "method": "GET",
            "headers": auth,
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_add_comment",
            "title": "Add Jira comment",
            "description": "Add an Atlassian Document Format comment to one Jira issue.",
            "url": f"{base}/rest/api/3/issue/{{key}}/comment",
            "method": "POST",
            "headers": auth,
            "approval": "always_approve",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "body": _ADF_SCHEMA,
                    "visibility": {"type": "object"},
                },
                "required": ["key", "body"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_transition",
            "title": "Transition Jira issue",
            "description": "Move one Jira issue through an available workflow transition.",
            "url": f"{base}/rest/api/3/issue/{{key}}/transitions",
            "method": "POST",
            "headers": auth,
            "approval": "always_approve",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "transition": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                    "fields": {"type": "object"},
                    "update": {"type": "object"},
                },
                "required": ["key", "transition"],
            },
        },
        {
            "kind": "http",
            "name": f"{name}_create_issue",
            "title": "Create Jira issue",
            "description": "Create a Jira issue from Jira's fields and properties request shape.",
            "url": f"{base}/rest/api/3/issue",
            "method": "POST",
            "headers": auth,
            "approval": "always_approve",
            "input_schema": {
                "type": "object",
                "properties": {"fields": issue_fields, "properties": properties},
                "required": ["fields"],
            },
        },
    ]


def expand(
    name: str,
    *,
    token_env: str | None = None,
    workspace: str | None = None,
    remote: bool = False,
    url: str | None = None,
    all_tools: bool = False,
) -> dict[str, Any]:
    if remote or all_tools:
        raise ConnectorError("jira does not support remote or all_tools")
    base, oauth = _api_base(url or "")
    if oauth:
        auth = {"Authorization": _credential(ENTRY, token_env or "JIRA_ACCESS_TOKEN", workspace)}
    else:
        # The stored "jira" connection holds an OAuth access token, which is not
        # a valid Basic password. API token mode reads the token and the email
        # from the environment only.
        if workspace:
            raise ConnectorError("jira takes a workspace only with the OAuth base "
                                 "https://api.atlassian.com/ex/jira/<cloudId>; a site URL uses "
                                 "JIRA_EMAIL and an API token from the environment")
        authorization = _credential(ENTRY, token_env, None, prefix="Basic ")
        authorization["basic_user"] = {"env": "JIRA_EMAIL"}
        auth = {"Authorization": authorization}
    sources = _sources(name, base, auth)
    for source in sources:
        source["approval_mode"] = "tighten"
        source["catalog"] = "jira"
    return {"sources": sources, "allow": [source["name"] for source in sources]}


def minimum(tool_name: str, method: str | None) -> str | None:
    """Keep Jira writes behind approval even if their declarations change."""
    if tool_name.endswith(("_add_comment", "_transition", "_create_issue")):
        return "always_approve"
    if method and method.upper() not in _READ_METHODS:
        return "always_approve"
    return None


def identity(
    token_response: Mapping[str, Any],
    get_json: Callable[[str, Mapping[str, str]], Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    """Return the only Jira site reached by an Atlassian OAuth token."""
    del get_json
    try:
        access_token = str(token_response["access_token"])
    except KeyError:
        raise ConnectorError("jira token response has no access token") from None
    from fabri.service import jira_oauth

    sites = list({site["id"]: site for site in jira_oauth.fetch_sites(access_token)}.values())
    if not sites:
        raise ConnectorError("the token reaches no Jira site")
    hosts = sorted({urlsplit(site.get("url", "")).hostname or "unknown" for site in sites})
    if len(sites) > 1:
        raise ConnectorError(
            f"the token reaches {len(sites)} Jira sites ({', '.join(hosts)}); "
            "revoke the extra grant or connect with an API token"
        )
    site = sites[0]
    host = urlsplit(site.get("url", "")).hostname or "Jira"
    return site["id"], site.get("name") or host
