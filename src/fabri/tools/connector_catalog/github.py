"""GitHub's official MCP server, local in Docker or the hosted endpoint."""
from __future__ import annotations

from typing import Any

from fabri.tools.connector_catalog import Connector, _credential

ENTRY = Connector(
    "github", "GitHub", "GitHub's official MCP server: issues, pull requests, code and actions.",
    ("mcp",), "GITHUB_PERSONAL_ACCESS_TOKEN", "github", "https://api.githubcopilot.com/mcp/")


# The hosted MCP endpoint. An http MCP source pointed here gets this
# connector's floor even with its markers removed. No http call to it is
# trusted as read, so READS is empty.
HOSTS = ("api.githubcopilot.com",)
READS: tuple[tuple[str, str], ...] = ()


def expand(name: str, *, token_env: str | None = None, workspace: str | None = None, remote: bool = False,
           url: str | None = None, all_tools: bool = False) -> dict[str, Any]:
    if not remote:
        if workspace:
            env_spec: Any = {"secret_ref": f"github:{workspace}"}
        else:
            env_spec = {"env": token_env or ENTRY.token_env}
        source = {"kind": "mcp", "name": name,
                  "description": ENTRY.summary,
                  "command": ["docker", "run", "--rm", "-i", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                              "ghcr.io/github/github-mcp-server"],
                  "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": env_spec},
                  "approval_mode": "tighten", "catalog": "github", "env_mode": "allowlist"}
        return {"sources": [source], "allow": [f"mcp_{name}_*"]}
    source = {"kind": "mcp", "name": name, "description": ENTRY.summary,
              "url": ENTRY.remote_url,
              "headers": {"Authorization": _credential(ENTRY, token_env, workspace)},
              "approval_mode": "tighten", "catalog": "github"}
    return {"sources": [source], "allow": [f"mcp_{name}_*"]}


def minimum(tool_name: str, method: str | None) -> str | None:
    return None
