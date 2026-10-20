"""Ready-made tool sources for common services.

A connector is a template that expands into plain source declarations
(the same shape as ``tools/tools.yaml``). Nothing here talks to the service;
the declarations are built like any other source when a setup resolves.
Credentials are referenced by environment variable name or by a
``provider:handle`` secret ref, never stored.

    github   GitHub's official MCP server, local in Docker or the hosted endpoint
    linear   Linear's hosted MCP server
    slack    three Slack Web API calls: list channels, read history, post a message
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ConnectorError(ValueError):
    """An unknown connector or a bad connector option."""


@dataclass(frozen=True)
class Connector:
    name: str
    title: str
    summary: str
    kinds: tuple[str, ...]  # source kinds it expands to; the policy must allow them
    token_env: str  # default environment variable that holds the credential
    secret_provider: str | None = None  # provider for --workspace secret refs
    remote_url: str | None = None


CONNECTORS: dict[str, Connector] = {
    "github": Connector(
        "github", "GitHub", "GitHub's official MCP server: issues, pull requests, code and actions.",
        ("mcp",), "GITHUB_PERSONAL_ACCESS_TOKEN", "github", "https://api.githubcopilot.com/mcp/"),
    "linear": Connector(
        "linear", "Linear", "Linear's hosted MCP server: issues, projects, comments and cycles.",
        ("mcp",), "LINEAR_API_KEY", "linear", "https://mcp.linear.app/mcp"),
    "slack": Connector(
        "slack", "Slack", "Slack Web API: list channels, read channel history, post a message.",
        ("http",), "SLACK_BOT_TOKEN", "slack"),
}

_SLACK = "https://slack.com/api"


def list_connectors() -> list[dict[str, Any]]:
    # Key names avoid "token" and "secret" so the API's response scrub leaves them alone.
    return [{"name": c.name, "title": c.title, "summary": c.summary, "kinds": list(c.kinds),
             "env_var": c.token_env, "store_provider": c.secret_provider,
             "remote_option": c.name == "github"} for c in CONNECTORS.values()]


def _credential(connector: Connector, token_env: str | None, workspace: str | None,
                prefix: str = "Bearer ") -> dict[str, Any]:
    if workspace:
        if not connector.secret_provider:
            raise ConnectorError(f"connector {connector.name} takes no workspace")
        spec: dict[str, Any] = {"secret_ref": f"{connector.secret_provider}:{workspace}"}
    else:
        spec = {"env": token_env or connector.token_env}
    if prefix:
        spec["prefix"] = prefix
    return spec


def connector_entry(
    kind: str,
    *,
    name: str | None = None,
    token_env: str | None = None,
    workspace: str | None = None,
    remote: bool = False,
) -> dict[str, Any]:
    """The ``{sources: [...], allow: [...]}`` mapping a connector expands to.
    `name` is the source name (default: the connector name); `token_env`
    replaces the default credential variable; `workspace` uses the secret ref
    ``<provider>:<workspace>`` instead; `remote` picks GitHub's hosted server."""
    connector = CONNECTORS.get(kind)
    if connector is None:
        raise ConnectorError(f"unknown connector {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    name = name or connector.name
    if remote and kind != "github":
        raise ConnectorError("--remote only applies to github; linear is always the hosted server")
    if kind == "github" and not remote:
        if workspace:
            env_spec: Any = {"secret_ref": f"github:{workspace}"}
        else:
            env_spec = {"env": token_env or connector.token_env}
        source = {"kind": "mcp", "name": name,
                  "description": connector.summary,
                  "command": ["docker", "run", "--rm", "-i", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                              "ghcr.io/github/github-mcp-server"],
                  "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": env_spec}}
        return {"sources": [source], "allow": [f"mcp_{name}_*"]}
    if kind in ("github", "linear"):
        source = {"kind": "mcp", "name": name, "description": connector.summary,
                  "url": connector.remote_url,
                  "headers": {"Authorization": _credential(connector, token_env, workspace)}}
        return {"sources": [source], "allow": [f"mcp_{name}_*"]}
    auth = {"Authorization": _credential(connector, token_env, workspace)}
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
    return {"sources": sources, "allow": [s["name"] for s in sources]}


def connector_kinds(entry: Mapping[str, Any]) -> set[str]:
    return {str(s.get("kind")) for s in entry.get("sources") or [] if isinstance(s, Mapping)}


__all__ = ["CONNECTORS", "Connector", "ConnectorError", "connector_entry", "connector_kinds", "list_connectors"]
