"""The Composio connector: written disabled, every tool always_approve."""
from __future__ import annotations

import pytest

from fabri.tools import connectors, mcp_client
from fabri.tools.connector_catalog import composio
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.sources import build_source_tools

URL = "https://example.test/mcp"


def _entry(**kw):
    return connectors.connector_entry("composio", url=URL, **kw)


@pytest.mark.parametrize("url", [None, "", "http://example.test/mcp", "ftp://example.test/x", "https://",
                                 "not a url"])
def test_expand_requires_https_url(url):
    with pytest.raises(connectors.ConnectorError):
        connectors.connector_entry("composio", url=url)


def test_include_required_exact_names():
    assert connectors.CONNECTORS["composio"].needs_include is True
    data = _entry()
    # The raw expansion is a placeholder pattern; the entry builder narrows it.
    assert data["allow"] == ["mcp_composio_*"]
    assert data["sources"][0]["url"] == URL


def _fake_build(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")
    class Client:
        pass

    def fake_build(cfg, approval_for=None):
        pairs = []
        for name in ("GMAIL_SEND_EMAIL", "GITHUB_LIST_ISSUES"):
            m = ToolManifest(name=name, description="x", command=[], input_schema={}, output_schema={},
                             approval="read")
            pairs.append((m, lambda args: {"ok": True}))
        return Client(), pairs

    monkeypatch.setattr(mcp_client, "build_mcp_tools", fake_build)


def test_written_disabled_and_every_tool_always_approve(monkeypatch):
    _fake_build(monkeypatch)
    source = _entry()["sources"][0]
    assert source["enabled"] is False
    assert source["approval"] == "always_approve"
    assert source["approval_mode"] == "tighten" and source["catalog"] == "composio"

    tools = build_source_tools(source)
    assert {t.manifest.name: t.manifest.approval for t in tools} == {
        "GMAIL_SEND_EMAIL": "always_approve", "GITHUB_LIST_ISSUES": "always_approve"}

    # Enabled, with a per-tool map that says read: the list tool stays always_approve.
    edited = {**source, "enabled": True, "approval": {"GITHUB_LIST_ISSUES": "read", "GMAIL_SEND_EMAIL": "read"}}
    tools = build_source_tools(edited)
    assert all(t.manifest.approval == "always_approve" for t in tools)

    # approval_mode removed: the catalog id alone still sets the floor.
    no_mode = {k: v for k, v in edited.items() if k != "approval_mode"}
    assert all(t.manifest.approval == "always_approve" for t in build_source_tools(no_mode))


def test_minimum_has_no_read_names_in_this_release():
    assert composio.READ_OK == ()
    assert connectors.catalog_minimum("composio", "GITHUB_LIST_ISSUES", None) == "always_approve"
    assert connectors.catalog_minimum("composio", "anything", "GET") == "always_approve"


def test_header_is_x_api_key_without_prefix():
    headers = _entry(workspace="acct1")["sources"][0]["headers"]
    assert headers == {"x-api-key": {"secret_ref": "composio:acct1"}}
    headers = _entry()["sources"][0]["headers"]
    assert headers == {"x-api-key": {"env": "COMPOSIO_API_KEY"}}
    assert _entry(token_env="MY_KEY")["sources"][0]["headers"] == {"x-api-key": {"env": "MY_KEY"}}


def test_list_connectors_marks_experimental():
    rows = {r["name"]: r for r in connectors.list_connectors()}
    assert rows["composio"]["experimental"] is True
    assert rows["composio"]["needs_url"] is True and rows["composio"]["needs_include"] is True
    assert rows["composio"]["auth_kind"] == "api_key"
    assert not any(r["experimental"] for n, r in rows.items() if n != "composio")
