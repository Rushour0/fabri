"""Linear's hosted MCP server."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fabri.tools.connector_catalog import Connector, ConnectorError, OAuthSpec, _credential

ENTRY = Connector(
    "linear", "Linear", "Linear's hosted MCP server: issues, projects, comments and cycles.",
    ("mcp",), "LINEAR_API_KEY", "linear", "https://mcp.linear.app/mcp",
    oauth=OAuthSpec(
        "https://linear.app/oauth/authorize", "https://api.linear.app/oauth/token", ("read", "write"),
        client_id_env="LINEAR_CLIENT_ID", secret_env="LINEAR_CLIENT_SECRET", pkce=False,
        extra_authorize={"actor": "application"}, callback_alias="/linear/oauth/callback",
        scope_separator=","))


# The hosted MCP endpoint. An http MCP source pointed here gets this
# connector's floor even with its markers removed. No http call to it is
# trusted as read, so READS is empty.
HOSTS = ("mcp.linear.app",)
READS: tuple[tuple[str, str], ...] = ()


def expand(name: str, *, token_env: str | None = None, workspace: str | None = None, remote: bool = False,
           url: str | None = None, all_tools: bool = False) -> dict[str, Any]:
    source = {"kind": "mcp", "name": name, "description": ENTRY.summary,
              "url": ENTRY.remote_url,
              "headers": {"Authorization": _credential(ENTRY, token_env, workspace)},
              "approval_mode": "tighten", "catalog": "linear"}
    return {"sources": [source], "allow": [f"mcp_{name}_*"]}


def minimum(tool_name: str, method: str | None) -> str | None:
    return None


def identity(token_response: Mapping[str, Any],
             get_json: Callable[[str, Mapping[str, str]], Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """The workspace (organization id, name) the token belongs to, asked of
    Linear's GraphQL API with the new token."""
    from fabri.service import linear_oauth

    workspace = linear_oauth.fetch_workspace(str(token_response["access_token"]))
    if not workspace.get("id"):
        raise ConnectorError("linear did not name the workspace for this token")
    name = workspace.get("name")
    return str(workspace["id"]), str(name) if name else None
