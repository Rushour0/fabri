"""The connector catalog package and the `fabri.tools.connectors` facade.

The snapshot fixture was captured from the v0.27.0 `connectors.py` before the
split. Catalog expansions now also carry `approval_mode`, `catalog` and (on a
stdio MCP source) `env_mode`; the snapshot test strips those three keys,
compares byte for byte, then checks the keys are present."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from fabri.tools import connector_catalog

FIXTURE = Path(__file__).parent / "fixtures" / "connectors" / "snapshot_v0_27_0.json"
NEW_KEYS = ("approval_mode", "catalog", "env_mode")


def _snapshot() -> dict:
    return json.loads(FIXTURE.read_text())


def test_three_entries_match_v0_27_0_snapshot():
    from fabri.tools.connectors import connector_entry

    cases = _snapshot()["cases"]
    assert len(cases) == 9
    for case in cases:
        entry = connector_entry(case["kind"], **case["kwargs"])
        stripped = json.loads(json.dumps(entry))
        for source in stripped["sources"]:
            for key in NEW_KEYS:
                source.pop(key, None)
        assert json.dumps(stripped, sort_keys=True) == case["entry"], case
        for source in entry["sources"]:
            assert source["approval_mode"] == "tighten"
            assert source["catalog"] == case["kind"]
            stdio = source["kind"] == "mcp" and "command" in source
            assert source.get("env_mode") == ("allowlist" if stdio else None), case
        # The old keys keep their order (to_yaml writes unsorted); the new keys come after them.
        for source, old in zip(entry["sources"], _old_insertion_order(case), strict=True):
            assert list(source)[:len(old)] == old, case
            assert set(list(source)[len(old):]) <= set(NEW_KEYS), case


def _old_insertion_order(case: dict) -> list[list[str]]:
    """Key order of each source as v0.27.0 wrote it (the module code, not the
    sorted fixture): mcp stdio, mcp hosted, or slack http."""
    if case["kind"] == "github" and not case["kwargs"].get("remote"):
        return [["kind", "name", "description", "command", "env"]]
    if case["kind"] in ("github", "linear"):
        return [["kind", "name", "description", "url", "headers"]]
    get = ["kind", "name", "title", "description", "url", "method", "headers", "input_schema"]
    post = ["kind", "name", "title", "description", "url", "method", "headers", "approval", "input_schema"]
    return [get, get, post]


def test_list_connectors_keeps_old_keys_and_order():
    from fabri.tools.connectors import list_connectors

    old = _snapshot()["list"]
    rows = list_connectors()
    assert [r["name"] for r in rows[:3]] == ["github", "linear", "slack"]
    for row, before in zip(rows[:3], old, strict=True):
        assert list(row)[:len(before)] == list(before)
        assert {k: row[k] for k in before} == before
        assert row["auth_kind"] == "pat" and row["curated"] is False and row["pinned_spec"] is False
        assert row["needs_include"] is False and row["needs_url"] is False and row["experimental"] is False
    assert all("token" not in k and "secret" not in k for r in rows for k in r)


def _write_module(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body)
    importlib.invalidate_caches()


_GOOD_HEADER = (
    "from fabri.tools.connector_catalog import Connector\n"
    "ENTRY = Connector({name!r}, 'T', 'S', ('mcp',), 'X_TOKEN')\n"
    "def minimum(tool_name, method):\n    return None\n"
)


@pytest.fixture
def temp_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_catalog, "__path__", [*connector_catalog.__path__, str(tmp_path)])
    names: list[str] = []

    def add(name: str, body: str) -> None:
        names.append(name)
        _write_module(tmp_path, name, body)

    yield add
    for name in names:
        sys.modules.pop(f"{connector_catalog.__name__}.{name}", None)


def test_loader_rejects_empty_allow(temp_catalog, monkeypatch):
    temp_catalog("zz_empty", _GOOD_HEADER.format(name="zz_empty") + (
        "def expand(name, *, token_env, workspace, remote, url, all_tools):\n"
        "    return {'sources': [{'kind': 'mcp', 'name': name, 'url': 'https://x.test',\n"
        "                         'approval_mode': 'tighten', 'catalog': 'zz_empty'}], 'allow': []}\n"))
    monkeypatch.setattr(connector_catalog, "MODULE_ORDER", ("zz_empty",))
    with pytest.raises(connector_catalog.ConnectorError, match="empty allow"):
        connector_catalog._load_modules()


def test_loader_checks_name_and_tags_and_skips_missing(temp_catalog, monkeypatch):
    temp_catalog("zz_misnamed", _GOOD_HEADER.format(name="other") + (
        "def expand(name, **kw):\n    return {'sources': [], 'allow': []}\n"))
    temp_catalog("zz_untagged", _GOOD_HEADER.format(name="zz_untagged") + (
        "def expand(name, **kw):\n"
        "    return {'sources': [{'kind': 'mcp', 'name': name}], 'allow': ['mcp_x_*']}\n"))
    temp_catalog("zz_broken", "import fabri_no_such_module_here\n")
    temp_catalog("zz_ok", _GOOD_HEADER.format(name="zz_ok") + (
        "def expand(name, **kw):\n"
        "    return {'sources': [{'kind': 'mcp', 'name': name, 'approval_mode': 'tighten',\n"
        "                         'catalog': 'zz_ok'}], 'allow': ['mcp_x_*']}\n"))
    with pytest.raises(connector_catalog.ConnectorError, match="ENTRY.name"):
        connector_catalog._load_modules(("zz_misnamed",))
    with pytest.raises(connector_catalog.ConnectorError, match="approval_mode: tighten"):
        connector_catalog._load_modules(("zz_untagged",))
    # An import error inside a module is not mistaken for a missing module.
    with pytest.raises(ModuleNotFoundError):
        connector_catalog._load_modules(("zz_broken",))
    assert list(connector_catalog._load_modules(("zz_not_written_yet", "zz_ok"))) == ["zz_ok"]


def test_facade_exports():
    from fabri.tools.connectors import (  # noqa: F401
        CONNECTORS,
        Connector,
        ConnectorError,
        catalog_minimum,
        connector_entry,
        connector_kinds,
        list_connectors,
        request_floor,
        spec_rel,
        spec_text,
    )
    import fabri.tools.connectors as facade

    assert CONNECTORS is connector_catalog.CONNECTORS
    assert list(CONNECTORS)[:3] == ["github", "linear", "slack"]
    assert set(facade.__all__) >= {"CONNECTORS", "Connector", "ConnectorError", "connector_entry",
                                   "connector_kinds", "list_connectors", "catalog_minimum", "spec_rel", "spec_text",
                                   "catalog_for_url", "request_floor"}
    assert connector_kinds(connector_entry("slack")) == {"http"}
    assert spec_text("slack") is None and spec_rel("github", "gh") is None


def test_connector_entry_option_errors():
    from fabri.tools.connectors import ConnectorError, connector_entry

    with pytest.raises(ConnectorError, match="--remote only applies to github"):
        connector_entry("linear", remote=True)
    with pytest.raises(ConnectorError, match="takes no url"):
        connector_entry("github", url="https://example.test/mcp")
    with pytest.raises(ConnectorError, match="all_tools"):
        connector_entry("slack", all_tools=True)
    with pytest.raises(ConnectorError, match="unknown connector 'jira'"):
        connector_entry("jira")


def test_catalog_minimum_slack():
    from fabri.tools.connectors import ConnectorError, catalog_minimum

    assert catalog_minimum("slack", "ops_post_message", "POST") == "always_approve"
    assert catalog_minimum("slack", "slack_post_message", None) == "always_approve"
    assert catalog_minimum("slack", "slack_channels_list", "GET") is None
    assert catalog_minimum("slack", "slack_channel_history", "GET") is None
    assert catalog_minimum("github", "mcp_github_create_issue", None) is None
    assert catalog_minimum("linear", "mcp_linear_save_issue", None) is None
    with pytest.raises(ConnectorError, match="unknown connector"):
        catalog_minimum("nope", "x", "GET")


def test_request_floor_by_host():
    from fabri.tools.connectors import catalog_for_url, request_floor

    assert request_floor("https://slack.com/api/chat.postMessage", "POST") == ("slack", "always_approve")
    assert request_floor("https://slack.com/api/chat.postMessage", "GET") == ("slack", "always_approve")
    assert request_floor("https://slack.com/api/conversations.list", "GET") == ("slack", None)
    assert request_floor("https://slack.com/api/conversations.history?limit=5", "get") == ("slack", None)
    assert request_floor("https://slack.com/api/conversations.list", "POST") == ("slack", "always_approve")
    assert request_floor("https://slack.com/api/conversations.listx", "GET") == ("slack", "always_approve")
    assert request_floor("https://files.slack.com/api/conversations.list", "GET") == ("slack", None)
    assert request_floor("https://evil.test/api/chat.postMessage", "POST") == (None, None)
    assert request_floor("https://notslack.com/api/chat.postMessage", "POST") == (None, None)
    # Either parser seeing slack.com is enough.
    assert catalog_for_url("https://evil.test\\@slack.com/api/chat.postMessage") == "slack"
    assert catalog_for_url("https://u:p@slack.com/api/x") == "slack"
    assert catalog_for_url("https://mcp.linear.app/mcp") == "linear"
    assert catalog_for_url("https://api.githubcopilot.com/mcp/") == "github"
    assert catalog_for_url(None) is None


def test_loader_checks_hosts_and_reads(temp_catalog):
    expand = ("def expand(name, **kw):\n"
              "    return {'sources': [{'kind': 'http', 'name': name, 'url': 'https://x.test',\n"
              "                         'approval_mode': 'tighten', 'catalog': MOD}], 'allow': [name]}\n")
    temp_catalog("zz_badhost", _GOOD_HEADER.format(name="zz_badhost") + expand.replace("MOD", "'zz_badhost'")
                 + "HOSTS = ('Slack.com',)\n")
    temp_catalog("zz_badreads", _GOOD_HEADER.format(name="zz_badreads") + expand.replace("MOD", "'zz_badreads'")
                 + "READS = (('get', '/x'),)\n")
    temp_catalog("zz_dup", _GOOD_HEADER.format(name="zz_dup") + expand.replace("MOD", "'zz_dup'")
                 + "HOSTS = ('slack.com',)\n")
    with pytest.raises(connector_catalog.ConnectorError, match="HOSTS"):
        connector_catalog._load_modules(("zz_badhost",))
    with pytest.raises(connector_catalog.ConnectorError, match="READS"):
        connector_catalog._load_modules(("zz_badreads",))
    with pytest.raises(connector_catalog.ConnectorError, match="both claim host slack.com"):
        connector_catalog._load_modules(("slack", "zz_dup"))


def test_setups_cli_connectors_and_route(monkeypatch, capsys, tmp_path):
    from fabri.cli import main
    from fabri.service.setups_routes import SetupsApi
    from fabri.tools.connectors import list_connectors

    monkeypatch.setattr(sys, "argv", ["fabri", "setups", "tools", "connectors", "--json"])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    rows = json.loads(capsys.readouterr().out)
    assert rows == list_connectors()
    assert [r["name"] for r in rows[:3]] == ["github", "linear", "slack"]

    status, body = SetupsApi(tmp_path).get_connectors(None)
    assert status == 200 and body == list_connectors()


_OAUTH_HEADER = (
    "from fabri.tools.connector_catalog import Connector, OAuthSpec\n"
    "ENTRY = Connector({name!r}, 'T', 'S', ('mcp',), 'X_TOKEN', oauth=OAuthSpec(\n"
    "    'https://a.test/authorize', 'https://a.test/token', ('read',), client_id_env='ZZ_ID'))\n"
    "def minimum(tool_name, method):\n    return None\n"
    "def expand(name, **kw):\n"
    "    return {{'sources': [{{'kind': 'mcp', 'name': name, 'approval_mode': 'tighten',\n"
    "                          'catalog': {name!r}}}], 'allow': ['mcp_x_*']}}\n"
)


def test_loader_needs_identity_when_oauth_is_set(temp_catalog):
    temp_catalog("zz_noident", _OAUTH_HEADER.format(name="zz_noident"))
    temp_catalog("zz_ident", _OAUTH_HEADER.format(name="zz_ident")
                 + "def identity(tok, get_json):\n    return None, None\n")
    with pytest.raises(connector_catalog.ConnectorError, match="identity must be callable"):
        connector_catalog._load_modules(("zz_noident",))
    assert list(connector_catalog._load_modules(("zz_ident",))) == ["zz_ident"]


def test_slack_and_linear_carry_oauth_and_expand_is_unchanged():
    for kind in ("slack", "linear"):
        entry = connector_catalog.CONNECTORS[kind]
        assert entry.oauth is not None and entry.oauth.client_id_env == f"{kind.upper()}_CLIENT_ID"
        assert entry.auth_kind == "pat"
        assert callable(connector_catalog.connector_module(kind).identity)
    assert connector_catalog.CONNECTORS["github"].oauth is None
