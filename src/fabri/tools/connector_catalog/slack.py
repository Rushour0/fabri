"""Three Slack Web API calls: list channels, read history, post a message."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from fabri.tools.connector_catalog import Connector, ConnectorError, OAuthSpec, _credential

# The bot scopes the Slack app asks for; the same list as
# fabri.service.slack_oauth.SLACK_BOT_SCOPES (that module is service side and
# is not imported here).
SLACK_BOT_SCOPES = ("chat:write", "channels:read", "app_mentions:read", "channels:history")

ENTRY = Connector(
    "slack", "Slack", "Slack Web API: list channels, read channel history, post a message.",
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
        {"kind": "http", "name": f"{name}_post_message", "title": "Post to Slack",
         "description": "Post a message to a channel, or reply in a thread with thread_ts.",
         "url": f"{_SLACK}/chat.postMessage", "method": "POST", "headers": auth, "approval": "always_approve",
         "input_schema": {"type": "object", "properties": {
             "channel": {"type": "string"}, "text": {"type": "string"},
             "thread_ts": {"type": "string"}}, "required": ["channel", "text"]}},
    ]
    for source in sources:
        source["approval_mode"] = "tighten"
        source["catalog"] = "slack"
    return {"sources": sources, "allow": [s["name"] for s in sources]}


def minimum(tool_name: str, method: str | None) -> str | None:
    """Posting a message always needs approval, and so does any call whose
    method was edited away from a read."""
    if tool_name.endswith("_post_message"):
        return "always_approve"
    if method and method.upper() not in _READ_METHODS:
        return "always_approve"
    return None


def identity(token_response: Mapping[str, Any],
             get_json: Callable[[str, Mapping[str, str]], Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """The workspace (team id, team name) from Slack's oauth.v2.access body.
    Slack answers a refused exchange with HTTP 200 and ``ok: false``; that
    raises with Slack's short error code only, never the body."""
    if not token_response.get("ok"):
        code = str(token_response.get("error") or "unknown")
        code = re.sub(r"[^a-z0-9_]", "", code.lower())[:60] or "unknown"
        raise ConnectorError(f"slack refused the code exchange ({code})")
    team = token_response.get("team")
    if not isinstance(team, Mapping) or not team.get("id"):
        raise ConnectorError("slack token response has no team id")
    name = team.get("name")
    return str(team["id"]), str(name) if name else None
