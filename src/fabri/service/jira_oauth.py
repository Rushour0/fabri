"""Resolve Jira sites available to an Atlassian OAuth token."""
from __future__ import annotations

import httpx

from fabri.tools import connections
from fabri.tools.connector_catalog import ConnectorError
from fabri.tools.security.ssrf import validate_url

ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"


def fetch_sites(access_token: str) -> list[dict[str, str]]:
    """Return Jira sites visible to an Atlassian access token."""
    try:
        validate_url(ACCESSIBLE_RESOURCES_URL)
    except ValueError:
        raise ConnectorError("jira site lookup URL was refused") from None
    try:
        with connections._client(connections._transport, 15.0) as client:
            response = client.get(
                ACCESSIBLE_RESOURCES_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ConnectorError(f"jira site lookup failed ({type(exc).__name__})") from None
    if not 200 <= response.status_code < 300:
        raise ConnectorError(f"jira site lookup failed (HTTP {response.status_code})")
    try:
        body = response.json()
    except ValueError:
        raise ConnectorError("jira site lookup returned a body that is not JSON") from None
    if not isinstance(body, list):
        raise ConnectorError("jira site lookup returned a body that is not a JSON list")

    sites: list[dict[str, str]] = []
    for item in body:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        scopes = item.get("scopes")
        if not isinstance(scopes, list) or not any(
            isinstance(scope, str) and (scope.endswith(":jira-work") or "jira" in scope.lower())
            for scope in scopes
        ):
            continue
        sites.append(
            {
                "id": item["id"],
                "name": item["name"] if isinstance(item.get("name"), str) else "",
                "url": item["url"] if isinstance(item.get("url"), str) else "",
            }
        )
    return sites
