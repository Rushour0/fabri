"""Ready-made tool sources for common services.

A connector is a template that expands into plain source declarations
(the same shape as ``tools/tools.yaml``). Nothing here talks to the service;
the declarations are built like any other source when a setup resolves.
Credentials are referenced by environment variable name or by a
``provider:handle`` secret ref, never stored.

    github     GitHub's official MCP server, local in Docker or the hosted endpoint
    linear     Linear's hosted MCP server
    slack      three Slack Web API calls: list channels, read history, post a message
    cakemail   a curated set of Cakemail API calls from a pinned spec; every write needs approval
    composio   Composio's hosted MCP server (experimental, written disabled)

This module is the stable import path. The connectors themselves live in
``fabri.tools.connector_catalog``, one module per connector.
"""
from __future__ import annotations

from fabri.tools.connector_catalog import (
    CONNECTORS,
    Connector,
    ConnectorError,
    catalog_for_url,
    catalog_minimum,
    connector_entry,
    connector_kinds,
    list_connectors,
    request_floor,
    spec_rel,
    spec_text,
)

__all__ = ["CONNECTORS", "Connector", "ConnectorError", "catalog_for_url", "catalog_minimum", "connector_entry",
           "connector_kinds", "list_connectors", "request_floor", "spec_rel", "spec_text"]
