"""Composio's hosted MCP server (experimental).

The expansion is written disabled (``enabled: false``), so nothing is built
until someone runs ``fabri setups tools enable composio``. The auth header
(``x-api-key``) and the data path are not verified against a live account in
this release. Every tool is ``always_approve``, whatever its name.

There is no ``HOSTS`` entry: the server URL is chosen by the caller, so the
floor is carried by the ``catalog: composio`` marker and ``minimum`` below.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fabri.tools.connector_catalog import Connector, ConnectorError, _credential

ENTRY = Connector(
    "composio", "Composio",
    "Composio hosted MCP (experimental): tools for the apps connected in your Composio account. "
    "Written disabled; every tool needs approval.",
    ("mcp",), "COMPOSIO_API_KEY", "composio", auth_header="x-api-key", auth_prefix="", auth_kind="api_key",
    needs_include=True, needs_url=True, experimental=True)

HOSTS: tuple[str, ...] = ()
READS: tuple[tuple[str, str], ...] = ()

# Exact tool names trusted as read. Empty in this release: no YAML edit can
# lower a Composio tool, and a later relaxation is a reviewed code change.
READ_OK: tuple[str, ...] = ()


def _check_url(url: str | None) -> str:
    if not url:
        raise ConnectorError("connector composio needs a server url (--url)")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise ConnectorError(f"composio url is not a valid URL: {url!r}") from None
    if parts.scheme != "https" or not host:
        raise ConnectorError("composio url must be an https URL with a host")
    return url


def expand(name: str, *, token_env: str | None = None, workspace: str | None = None, remote: bool = False,
           url: str | None = None, all_tools: bool = False) -> dict[str, Any]:
    source = {"kind": "mcp", "name": name, "enabled": False,
              "description": "Composio hosted MCP (experimental): tools for the apps connected in your "
                             "Composio account. Auth header and data path unverified.",
              "url": _check_url(url),
              "headers": {ENTRY.auth_header: _credential(ENTRY, token_env, workspace)},
              "approval": "always_approve", "approval_mode": "tighten", "catalog": "composio"}
    # The loader check needs a non-empty allow. connector_tool_entry replaces
    # it with the exact tool names the caller passed to --include.
    return {"sources": [source], "allow": [f"mcp_{name}_*"]}


def minimum(tool_name: str, method: str | None) -> str | None:
    return None if tool_name in READ_OK else "always_approve"
