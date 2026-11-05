"""connector_tool_entry: include, all_tools and pinned spec handling."""
from __future__ import annotations

import json

import pytest

from fabri.setups import tool_entries
from fabri.setups.tool_entries import EntryError, connector_tool_entry
from fabri.tools.connectors import CONNECTORS, Connector

SPEC = {"openapi": "3.0.0", "info": {"title": "Mail API", "version": "1"},
        "servers": [{"url": "https://api.example.test"}],
        "paths": {"/things": {"get": {"operationId": "listThings", "responses": {"200": {"description": "ok"}}}}}}


def _fake(monkeypatch, *, kind="fakeapi", how="openapi", needs_include=False, spec=True):
    connector = Connector(kind, "Fake", "A fake connector.", (how,), "FAKE_TOKEN",
                          spec_file="fake.json" if spec else None, needs_include=needs_include)
    monkeypatch.setitem(CONNECTORS, kind, connector)

    def fake_entry(k, *, name=None, token_env=None, workspace=None, remote=False, url=None, all_tools=False):
        source = {"kind": how, "name": name}
        if how == "openapi":
            source["spec"] = f"specs/{name}.json"
        return {"sources": [source], "allow": ["curated_one"]}

    monkeypatch.setattr(tool_entries, "connector_entry", fake_entry)
    monkeypatch.setattr(tool_entries, "_catalog_spec_rel", lambda k, n: f"specs/{n}.json" if spec else None)
    monkeypatch.setattr(tool_entries, "_catalog_spec_text", lambda k: json.dumps(SPEC))
    return kind


def test_include_and_all_tools_conflict(monkeypatch):
    kind = _fake(monkeypatch)
    with pytest.raises(EntryError, match="cannot be combined"):
        connector_tool_entry(kind, include=["listThings"], all_tools=True)


def test_needs_include_refused_and_wildcards_refused(monkeypatch):
    kind = _fake(monkeypatch, how="mcp", needs_include=True, spec=False)
    with pytest.raises(EntryError, match="pass --include with exact tool names"):
        connector_tool_entry(kind)
    for bad in ("GMAIL_*", "a?b", "x[1]"):
        with pytest.raises(EntryError, match="exact tool names"):
            connector_tool_entry(kind, include=[bad])
    with pytest.raises(EntryError):
        connector_tool_entry(kind, all_tools=True)
    entry = connector_tool_entry(kind, include=["GMAIL_SEND_EMAIL"], name="gm")
    assert entry.data["allow"] == ["mcp_gm_GMAIL_SEND_EMAIL"]


def test_spec_fields_filled_for_pinned_spec(monkeypatch):
    kind = _fake(monkeypatch)
    entry = connector_tool_entry(kind, name="mailx")
    assert entry.spec_rel == "specs/mailx.json"
    assert json.loads(entry.spec_text) == SPEC
    assert entry.spec_doc["info"]["title"] == "Mail API"
    assert entry.data["allow"] == ["curated_one"]
    assert connector_tool_entry(kind, include=["listThings"]).data["allow"] == ["listThings"]
    assert connector_tool_entry(kind, all_tools=True).data["allow"] == ["*"]
