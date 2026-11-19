"""Slack reads for channels, threads, links and users, plus post and edit."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from fabri.tools.connector_catalog import Connector, ConnectorError, OAuthSpec, _credential

# The bot scopes the Slack app asks for; the same list as
# fabri.service.slack_oauth.SLACK_BOT_SCOPES (that module is service side and
# is not imported here).
SLACK_BOT_SCOPES = ("chat:write", "channels:read", "app_mentions:read", "channels:history", "groups:history",
                    "users:read", "users:read.email")

ENTRY = Connector(
    "slack", "Slack", "Slack Web API: read channels, threads, links and users, then post or edit messages.",
    ("http",), "SLACK_BOT_TOKEN", "slack",
    oauth=OAuthSpec(
        "https://slack.com/oauth/v2/authorize", "https://slack.com/api/oauth.v2.access",
        scopes_default=SLACK_BOT_SCOPES, client_id_env="SLACK_CLIENT_ID",
        secret_env="SLACK_CLIENT_SECRET", pkce=False, refresh=False,
        callback_alias="/slack/oauth/callback", scope_separator=","))

_SLACK = "https://slack.com/api"
_READ_METHODS = frozenset({"GET", "HEAD"})

# Any request to these hosts is always_approve unless it is listed in READS,
# whatever the tool is called and whatever the YAML says.
HOSTS = ("slack.com",)
READS = (
    ("GET", r"/api/conversations\.list"),
    ("GET", r"/api/conversations\.history"),
    ("GET", r"/api/conversations\.replies"),
    ("GET", r"/api/chat\.getPermalink"),
    ("GET", r"/api/users\.info"),
    ("GET", r"/api/users\.lookupByEmail"),
)


def expand(name: str, *, token_env: str | None = None, workspace: str | None = None, remote: bool = False,
           url: str | None = None, all_tools: bool = False) -> dict[str, Any]:
    auth = {"Authorization": _credential(ENTRY, token_env, workspace)}
    sources = [
        {"kind": "http", "name": f"{name}_channels_list", "title": "List Slack channels",
         "description": "List the Slack channels the bot can see. Returns ids and names; page with cursor.",
         "url": f"{_SLACK}/conversations.list", "method": "GET", "headers": auth,
         "input_schema": {"type": "object", "properties": {
             "limit": {"type": "integer", "maximum": 1000},
             "cursor": {"type": "string"},
             "types": {"type": "string", "description": "Comma list: public_channel, private_channel"}}}},
        {"kind": "http", "name": f"{name}_channel_history", "title": "Read a Slack channel",
         "description": "Recent messages in one channel, newest first.",
         "url": f"{_SLACK}/conversations.history", "method": "GET", "headers": auth,
         "input_schema": {"type": "object", "properties": {
             "channel": {"type": "string", "description": "Channel id, for example C0123456"},
             "limit": {"type": "integer", "maximum": 1000},
             "cursor": {"type": "string"}}, "required": ["channel"]}},
        {"kind": "http", "name": f"{name}_thread_replies", "title": "Read a Slack thread",
         "description": "Every message in one thread, parent first. Page with cursor.",
         "url": f"{_SLACK}/conversations.replies", "method": "GET", "headers": auth,
         "input_schema": {"type": "object", "properties": {
             "channel": {"type": "string"},
             "ts": {"type": "string", "description": "Timestamp of the thread's parent message"},
             "limit": {"type": "integer", "maximum": 1000},
             "cursor": {"type": "string"}}, "required": ["channel", "ts"]}},
        {"kind": "http", "name": f"{name}_permalink", "title": "Get a Slack message link",
         "description": "The permanent link to one message.",
         "url": f"{_SLACK}/chat.getPermalink", "method": "GET", "headers": auth,
         "input_schema": {"type": "object", "properties": {
             "channel": {"type": "string"},
             "message_ts": {"type": "string"}}, "required": ["channel", "message_ts"]}},
        {"kind": "http", "name": f"{name}_user_info", "title": "Look up a Slack user",
         "description": "One user's profile: name, display name, bot and guest flags.",
         "url": f"{_SLACK}/users.info", "method": "GET", "headers": auth,
         "input_schema": {"type": "object", "properties": {
             "user": {"type": "string", "description": "User id, for example U0123456"}},
             "required": ["user"]}},
        {"kind": "http", "name": f"{name}_user_by_email", "title": "Find a Slack user by email",
         "description": "The Slack user with this email address, if any.",
         "url": f"{_SLACK}/users.lookupByEmail", "method": "GET", "headers": auth,
         "input_schema": {"type": "object", "properties": {
             "email": {"type": "string"}}, "required": ["email"]}},
        {"kind": "http", "name": f"{name}_post_message", "title": "Post to Slack",
         "description": "Post a message to a channel, or reply in a thread with thread_ts.",
         "url": f"{_SLACK}/chat.postMessage", "method": "POST", "headers": auth, "approval": "always_approve",
         "input_schema": {"type": "object", "properties": {
             "channel": {"type": "string"}, "text": {"type": "string"},
             "thread_ts": {"type": "string"},
             "blocks": {"type": "array", "items": {"type": "object"},
                        "description": "Block Kit blocks; text stays the notification fallback"}},
             "required": ["channel", "text"]}},
        {"kind": "http", "name": f"{name}_update_message", "title": "Edit a Slack message",
         "description": "Replace the text or blocks of a message the bot posted.",
         "url": f"{_SLACK}/chat.update", "method": "POST", "headers": auth, "approval": "always_approve",
         "input_schema": {"type": "object", "properties": {
             "channel": {"type": "string"},
             "ts": {"type": "string", "description": "Timestamp of the message to edit"},
             "text": {"type": "string"},
             "blocks": {"type": "array", "items": {"type": "object"},
                        "description": "Block Kit blocks; text stays the notification fallback"}},
             "required": ["channel", "ts"]}},
    ]
    for source in sources:
        source["approval_mode"] = "tighten"
        source["catalog"] = "slack"
    return {"sources": sources, "allow": [s["name"] for s in sources]}


def minimum(tool_name: str, method: str | None) -> str | None:
    """Posting or editing a message always needs approval, as does a call whose
    method was edited away from a read."""
    if tool_name.endswith(("_post_message", "_update_message")):
        return "always_approve"
    if method and method.upper() not in _READ_METHODS:
        return "always_approve"
    return None


def _error_code(value: object) -> str:
    code = str(value or "unknown")
    return re.sub(r"[^a-z0-9_]", "", code.lower())[:60] or "unknown"


def shape_body(path: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of a request body with ``link_names`` and ``parse`` removed, and
    unfurls off for posts and edits, so outbound text cannot turn into mass
    mentions or link previews whatever arguments the caller passed."""
    shaped = {key: value for key, value in body.items() if key not in ("link_names", "parse")}
    if path in ("/api/chat.postMessage", "/api/chat.update"):
        shaped["unfurl_links"] = False
        shaped["unfurl_media"] = False
    return shaped


def response_error(data: object) -> str | None:
    """Slack reports most failures as HTTP 200 with ``ok: false``. Return
    Slack's short error code as the tool error, never the rest of the body."""
    if isinstance(data, Mapping) and data.get("ok") is False:
        return f"slack error ({_error_code(data.get('error'))})"
    return None


def identity(token_response: Mapping[str, Any],
             get_json: Callable[[str, Mapping[str, str]], Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """The workspace (team id, team name) from Slack's oauth.v2.access body.
    Slack answers a refused exchange with HTTP 200 and ``ok: false``; that
    raises with Slack's short error code only, never the body."""
    if not token_response.get("ok"):
        raise ConnectorError(f"slack refused the code exchange ({_error_code(token_response.get('error'))})")
    team = token_response.get("team")
    if not isinstance(team, Mapping) or not team.get("id"):
        raise ConnectorError("slack token response has no team id")
    name = team.get("name")
    return str(team["id"]), str(name) if name else None
