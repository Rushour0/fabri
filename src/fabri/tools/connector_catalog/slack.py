"""Three Slack Web API calls: list channels, read history, post a message."""
from __future__ import annotations

from typing import Any

from fabri.tools.connector_catalog import Connector, _credential

ENTRY = Connector(
    "slack", "Slack", "Slack Web API: list channels, read channel history, post a message.",
    ("http",), "SLACK_BOT_TOKEN", "slack")

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
