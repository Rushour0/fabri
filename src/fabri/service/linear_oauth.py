"""Linear OAuth helpers for the multi-tenant "connect your workspace" flow.

Mirrors ``slack_oauth`` (reusing its provider-neutral signed-state helpers) but
for Linear's OAuth v2: build the authorize redirect, exchange the code for a
per-workspace access token, and resolve the workspace id/name.
"""
import json
import os
import urllib.parse
import urllib.request

from fabri.integrations import linear as linear_api
from fabri.service.slack_oauth import public_base_url, sign_state, verify_state  # noqa: F401
from fabri.tools.security.ssrf import ValidatingRedirect, validate_url

LINEAR_AUTHORIZE = "https://linear.app/oauth/authorize"
LINEAR_TOKEN = "https://api.linear.app/oauth/token"
LINEAR_SCOPES = "read,write"

_opener = urllib.request.build_opener(ValidatingRedirect)


def build_install_redirect() -> str | None:
    """Authorize URL with a signed state, or None when unconfigured."""
    client_id = os.environ.get("LINEAR_CLIENT_ID", "")
    base = public_base_url()
    secret = os.environ.get("LINEAR_CLIENT_SECRET", "")
    if not client_id or not base or not secret:
        return None
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": f"{base}/linear/oauth/callback",
            "response_type": "code",
            "scope": LINEAR_SCOPES,
            "actor": "application",
            "state": sign_state(secret),
        }
    )
    return f"{LINEAR_AUTHORIZE}?{query}"


def exchange_code(code: str) -> dict:
    """Exchange the OAuth code for a per-workspace access token."""
    base = public_base_url()
    body = urllib.parse.urlencode(
        {
            "client_id": os.environ.get("LINEAR_CLIENT_ID", ""),
            "client_secret": os.environ.get("LINEAR_CLIENT_SECRET", ""),
            "code": code,
            "redirect_uri": f"{base}/linear/oauth/callback",
            "grant_type": "authorization_code",
        }
    ).encode()
    request = urllib.request.Request(
        LINEAR_TOKEN,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    validate_url(LINEAR_TOKEN)
    with _opener.open(request, timeout=10) as response:
        return json.loads(response.read().decode())


def fetch_workspace(access_token: str) -> dict:
    """Resolve the installing workspace's id + name from the access token."""
    data = linear_api._graphql(
        "query { organization { id name } }",
        {},
        token=access_token,
        auth_scheme="bearer",
    )
    org = (data or {}).get("organization") or {}
    return {"id": org.get("id"), "name": org.get("name")}
