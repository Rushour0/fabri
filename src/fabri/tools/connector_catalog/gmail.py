"""Gmail through Google's REST API: send mail and list labels by default;
drafts and reads are opt-in.

Send and draft are plain ``http`` sources with ``body_format: rfc822``. They
post one ``message/rfc822`` body to Gmail's media upload endpoints, so under
``fabri serve`` they go through the credential broker like any other http
tool, and the approval card shows the recipients, the subject and the body
as text. Both are always_approve, and nothing in the written YAML can lower
that: every non-read call to ``gmail.googleapis.com`` is floored by host.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from fabri.tools.connector_catalog import Connector, ConnectorError, OAuthSpec, _credential

_SCOPE = "https://www.googleapis.com/auth/"

# Short names a user can ask for at connect time (fabri connect gmail
# --scopes readonly). All three are restricted scopes: an app published to
# outside users needs Google's verification and a CASA security assessment
# before it may ask for any of them. compose is what drafts need; gmail.send
# does not cover drafts. The default set (send, labels, openid, email) holds
# no restricted scope.
SCOPES_OPTIONAL = {
    "readonly": _SCOPE + "gmail.readonly",
    "compose": _SCOPE + "gmail.compose",
    "modify": _SCOPE + "gmail.modify",
}
SCOPES_DEFAULT = ("openid", "email", _SCOPE + "gmail.send", _SCOPE + "gmail.labels")

# Tool name suffixes. The curated list needs only the default scopes.
_DEFAULT_TOOLS = ("labels_list", "messages_send")
_OPT_IN_TOOLS = ("drafts_create", "messages_search", "message_get")

ENTRY = Connector(
    "gmail", "Gmail", "Gmail: send mail as the connected user and list labels; drafts and reads are opt-in.",
    ("http",), "GMAIL_TOKEN", "gmail", "https://gmail.googleapis.com",
    auth_kind="oauth", scope_default="user", verify_path="/gmail/v1/users/me/labels",
    default_allow=tuple(f"gmail_{t}" for t in _DEFAULT_TOOLS),
    oauth=OAuthSpec(
        "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
        scopes_default=SCOPES_DEFAULT, scopes_optional=SCOPES_OPTIONAL,
        client_id_env="GOOGLE_CLIENT_ID", secret_env="GOOGLE_CLIENT_SECRET", pkce=True, loopback=True,
        extra_authorize={"access_type": "offline", "prompt": "consent"},
        identity_url="https://openidconnect.googleapis.com/v1/userinfo"))

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
_READ_METHODS = frozenset({"GET", "HEAD"})

# Any request to this host is always_approve unless it is listed here,
# whatever the tool is called and whatever the YAML says. The upload paths
# (send, drafts) are POSTs and so never read.
HOSTS = ("gmail.googleapis.com",)
READS = (
    ("GET", r"/gmail/v1/users/me/labels"),
    ("GET", r"/gmail/v1/users/me/messages"),
    # The template's {id}, or a rendered message id (Gmail ids are hex).
    ("GET", r"/gmail/v1/users/me/messages/(\{id\}|[A-Za-z0-9_-]+)"),
    ("GET", r"/gmail/v1/users/me/profile"),
)

_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipients, comma separated: a@example.com, B <b@example.com>"},
        "cc": {"type": "string", "description": "Cc recipients, comma separated"},
        "bcc": {"type": "string", "description": "Bcc recipients, comma separated"},
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Plain text body"},
    },
    "required": ["to", "subject", "body"],
    "additionalProperties": False,
}


def _sources(name: str, auth: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every Gmail source, keyed by tool name suffix."""
    return {
        "labels_list": {
            "kind": "http", "name": f"{name}_labels_list", "title": "List Gmail labels",
            "description": "List the labels in the connected mailbox (ids and names).",
            "url": f"{_API}/labels", "method": "GET", "headers": auth,
            "input_schema": {"type": "object", "properties": {}}},
        "messages_send": {
            "kind": "http", "name": f"{name}_messages_send", "title": "Send a Gmail message",
            "description": "Send a plain text email from the connected account. A person approves every send "
                           "after reading the recipients, subject and body.",
            "url": f"{_UPLOAD}/messages/send", "method": "POST", "query": {"uploadType": "media"},
            "body_format": "rfc822", "headers": auth, "approval": "always_approve",
            "input_schema": _MESSAGE_SCHEMA},
        "drafts_create": {
            "kind": "http", "name": f"{name}_drafts_create", "title": "Create a Gmail draft",
            "description": "Save a plain text draft in the connected account without sending it. Needs the "
                           "compose scope, which Google treats as restricted.",
            "url": f"{_UPLOAD}/drafts", "method": "POST", "query": {"uploadType": "media"},
            "body_format": "rfc822", "headers": auth, "approval": "always_approve",
            "input_schema": _MESSAGE_SCHEMA},
        "messages_search": {
            "kind": "http", "name": f"{name}_messages_search", "title": "Search Gmail",
            "description": "Find message ids with a Gmail search query (the same syntax as the Gmail search "
                           "box). Needs the readonly scope.",
            "url": f"{_API}/messages", "method": "GET", "headers": auth,
            "input_schema": {"type": "object", "properties": {
                "q": {"type": "string", "description": "Gmail search, for example from:a@example.com newer_than:7d"},
                "maxResults": {"type": "integer", "maximum": 500},
                "pageToken": {"type": "string"}}}},
        "message_get": {
            "kind": "http", "name": f"{name}_message_get", "title": "Read a Gmail message",
            "description": "One message by id. format=metadata returns headers only; full returns the body. "
                           "Needs the readonly scope.",
            "url": f"{_API}/messages/{{id}}", "method": "GET", "headers": auth,
            "input_schema": {"type": "object", "properties": {
                "id": {"type": "string"},
                "format": {"type": "string", "enum": ["minimal", "metadata", "full"]}},
                "required": ["id"]}},
    }


def expand(name: str, *, token_env: str | None = None, workspace: str | None = None, remote: bool = False,
           url: str | None = None, all_tools: bool = False) -> dict[str, Any]:
    auth = {"Authorization": _credential(ENTRY, token_env, workspace)}
    every = _sources(name, auth)
    picked = (*_DEFAULT_TOOLS, *_OPT_IN_TOOLS) if all_tools else _DEFAULT_TOOLS
    sources = [every[suffix] for suffix in picked]
    for source in sources:
        source["approval_mode"] = "tighten"
        source["catalog"] = "gmail"
    return {"sources": sources, "allow": [s["name"] for s in sources]}


def minimum(tool_name: str, method: str | None) -> str | None:
    """Sending and drafting always need approval, and so does any call whose
    method was edited away from a read."""
    if tool_name.endswith(("_messages_send", "_drafts_create")):
        return "always_approve"
    if method and method.upper() not in _READ_METHODS:
        return "always_approve"
    return None


_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


def identity(token_response: Mapping[str, Any],
             get_json: Callable[[str, Mapping[str, str]], Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """The account's email address, asked of Google's OpenID userinfo
    endpoint with the new token. The display name is the address. For an
    org connection the handle is the address's local part in lower case;
    a user connection is keyed by the user id instead."""
    spec = ENTRY.oauth
    assert spec is not None and spec.identity_url
    access = token_response.get("access_token")
    if not isinstance(access, str) or not access:
        raise ConnectorError("gmail token response has no access token")
    info = get_json(spec.identity_url, {"Authorization": "Bearer " + access})
    email = info.get("email") if isinstance(info, Mapping) else None
    if not isinstance(email, str) or "@" not in email:
        raise ConnectorError("google did not return the account email; the email scope is needed")
    local = re.sub(r"[^a-z0-9_.-]", "", email.split("@", 1)[0].lower())
    handle = local[:80] if _HANDLE.fullmatch(local[:80] or "-") else None
    return handle, email
