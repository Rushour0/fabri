"""Recorded tests for the Jira connector and its OAuth site lookup."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from fabri.service.connection_store import ConnectionStore
from fabri.setups import clear_cache
from fabri.setups.resolver import resolve_setup
from fabri.setups.toolset import resolve_tools
from fabri.tools import connections
from fabri.tools.connections.oauth import OAuthCodeProvider
from fabri.tools.connector_catalog import (
    CONNECTORS,
    ConnectorError,
    connector_entry,
    connector_module,
    jira,
)
from fabri.tools.connectors import list_connectors, request_floor
from fabri.tools.registry import ToolRegistry
from fabri.tools.sources import build_source_tools, register_tool_sources

pytestmark = pytest.mark.unit

EMAIL = "bot@example.com"
API_TOKEN = "jira-token"
ACCESS_TOKEN = "jira-access-token"
BASIC_AUTH = "Basic Ym90QGV4YW1wbGUuY29tOmppcmEtdG9rZW4="
CLOUD_ID = "cloud-123"
SITE_BASE = "https://acme.atlassian.net"
OAUTH_BASE = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"

ADF_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Recorded issue"}]}],
}
ISSUE_RESPONSE = {
    "id": "10012",
    "key": "PROJ-12",
    "fields": {"summary": "Recorded issue", "description": ADF_DESCRIPTION, "labels": ["triage"]},
}
COMMENTS_PAGE_1 = {
    "startAt": 0,
    "maxResults": 1,
    "total": 2,
    "comments": [{"id": "20001", "body": ADF_DESCRIPTION}],
}
COMMENTS_PAGE_2 = {
    "startAt": 1,
    "maxResults": 1,
    "total": 2,
    "comments": [{"id": "20002", "body": ADF_DESCRIPTION}],
}
SEARCH_PAGE_1 = {
    "issues": [{"id": "10012", "key": "PROJ-12", "fields": {"summary": "Recorded issue"}}],
    "nextPageToken": "page-two",
}
SEARCH_PAGE_2 = {
    "issues": [{"id": "10013", "key": "PROJ-13", "fields": {"summary": "Second issue"}}],
}
ACCESSIBLE_RESOURCES = [
    {
        "id": CLOUD_ID,
        "name": "Acme Jira",
        "url": SITE_BASE,
        "scopes": ["read:jira-work", "write:jira-work"],
    }
]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    clear_cache()

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    for name in list(os.environ):
        if name.startswith("FABRI_CRED_"):
            monkeypatch.delenv(name, raising=False)
    for name in (
        "JIRA_API_TOKEN",
        "JIRA_ACCESS_TOKEN",
        "JIRA_EMAIL",
        "JIRA_CLIENT_ID",
        "JIRA_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    clear_cache()


class Recorder:
    def __init__(self, responder=None) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder or (lambda request: httpx.Response(200, json={"ok": True}))
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def _tool(sources: list[dict], name: str, transport: httpx.BaseTransport):
    for source in sources:
        if source["name"] == name:
            return build_source_tools(source, transport=transport)[0]
    raise AssertionError(name)


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def _write_layer(tmp_path: Path, entry: dict, name: str) -> Path:
    layer = tmp_path / name
    (layer / "tools").mkdir(parents=True)
    (layer / "tools" / "tools.yaml").write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")
    return layer


def _resolved_tools(tmp_path: Path, entry: dict, name: str):
    layer = _write_layer(tmp_path, entry, name)
    return resolve_tools(resolve_setup([layer]), use_cache=False, catalog=False).tools


def test_catalog_loads_jira_with_expected_oauth_spec():
    entry = CONNECTORS["jira"]
    assert entry is jira.ENTRY and entry.name == "jira" and entry.kinds == ("http",)
    assert entry.auth_kind == "pat" and entry.scope_default == "org" and entry.needs_url
    assert "jira" in CONNECTORS and "jira" in {row["name"] for row in list_connectors()}
    oauth = entry.oauth
    assert oauth is not None
    assert oauth.authorize_url == "https://auth.atlassian.com/authorize"
    assert oauth.token_url == "https://auth.atlassian.com/oauth/token"
    assert oauth.scopes_default == ("read:jira-work", "offline_access")
    assert oauth.scopes_optional == {"write": "write:jira-work"}
    assert oauth.client_id_env == "JIRA_CLIENT_ID" and oauth.secret_env == "JIRA_CLIENT_SECRET"
    assert oauth.pkce is False and oauth.extra_authorize == {
        "audience": "api.atlassian.com",
        "prompt": "consent",
    }
    assert oauth.identity_url == jira.ACCESSIBLE_RESOURCES_URL and oauth.scope_separator == " "

    owners: dict[str, str] = {}
    for connector_name in CONNECTORS:
        for host in getattr(connector_module(connector_name), "HOSTS", ()):
            assert host not in owners, (host, owners[host], connector_name)
            owners[host] = connector_name
    assert owners["api.atlassian.com"] == "jira" and owners["atlassian.net"] == "jira"


@pytest.mark.parametrize(
    "url, workspace, expected_header",
    [
        (
            OAUTH_BASE + "/",
            "main",
            {"secret_ref": "jira:main", "prefix": "Bearer "},
        ),
        (
            "https://ACME.atlassian.net/",
            None,
            {
                "env": "JIRA_API_TOKEN",
                "prefix": "Basic ",
                "basic_user": {"env": "JIRA_EMAIL"},
            },
        ),
    ],
    ids=["oauth", "api-token"],
)
def test_expand_builds_eight_tightened_sources(url, workspace, expected_header):
    expanded = connector_entry("jira", name="work", workspace=workspace, url=url)
    assert len(expanded["sources"]) == 8
    assert expanded["allow"] == [source["name"] for source in expanded["sources"]]
    assert len(set(expanded["allow"])) == 8
    for source in expanded["sources"]:
        assert source["approval_mode"] == "tighten" and source["catalog"] == "jira"
        assert source["headers"] == {"Authorization": expected_header}
    writes = {"work_add_comment", "work_transition", "work_create_issue"}
    assert {source["name"] for source in expanded["sources"] if source.get("approval") == "always_approve"} == writes


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.atlassian.net",
        "https://example.com",
        "https://acme.atlassian.net/rest/api/3",
        "https://api.atlassian.com",
        "https://user@acme.atlassian.net",
        "https://acme.atlassian.net:443",
        "https://atlassian.net.evil.com",
    ],
    ids=["http", "other-host", "site-path", "missing-cloud", "userinfo", "port", "lookalike"],
)
def test_expand_refuses_bad_urls(url):
    with pytest.raises(ConnectorError, match="jira url must"):
        connector_entry("jira", url=url)


@pytest.mark.parametrize("option", ["remote", "all_tools"], ids=["remote", "all-tools"])
def test_expand_refuses_unsupported_options(option):
    kwargs = {"remote": False, "all_tools": False}
    kwargs[option] = True
    with pytest.raises(ConnectorError, match="does not support"):
        jira.expand("jira", url=SITE_BASE, **kwargs)


READ_PATHS = [
    ("get-template", "/rest/api/3/issue/{key}"),
    ("get-real", "/rest/api/3/issue/PROJ-12"),
    ("search", "/rest/api/3/search/jql"),
    ("comments-template", "/rest/api/3/issue/{key}/comment"),
    ("comments-real", "/rest/api/3/issue/PROJ-12/comment"),
    ("links-template", "/rest/api/3/issue/{key}/remotelink"),
    ("links-real", "/rest/api/3/issue/PROJ-12/remotelink"),
    ("transitions-template", "/rest/api/3/issue/{key}/transitions"),
    ("transitions-real", "/rest/api/3/issue/PROJ-12/transitions"),
]


@pytest.mark.parametrize("base", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
@pytest.mark.parametrize("path", [path for _, path in READ_PATHS], ids=[name for name, _ in READ_PATHS])
def test_request_floor_allows_only_listed_gets(base, path):
    assert request_floor(base + path, "GET") == ("jira", None)


@pytest.mark.parametrize("base", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"], ids=["post", "put", "delete"])
@pytest.mark.parametrize("path", [path for _, path in READ_PATHS], ids=[name for name, _ in READ_PATHS])
def test_request_floor_blocks_non_gets_on_read_paths(base, method, path):
    assert request_floor(base + path, method) == ("jira", "always_approve")


@pytest.mark.parametrize("base", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/3/issue/PROJ-1/worklog",
        "/wiki/rest/api/content",
        "/rest/api/3/myself",
        "/x/rest/api/3/search/jql",
        "/rest/api/3/search/jql/extra",
    ],
    ids=["worklog", "wiki", "myself", "prefix-substring", "suffix-substring"],
)
def test_request_floor_blocks_unlisted_gets(base, path):
    assert request_floor(base + path, "GET") == ("jira", "always_approve")


REQUEST_CASES = [
    (
        "get_issue",
        {"key": "PROJ-12", "fields": "summary,description", "expand": "names"},
        "GET",
        "/rest/api/3/issue/PROJ-12",
        {"fields": "summary,description", "expand": "names"},
        None,
    ),
    (
        "search",
        {"jql": "project = PROJ", "fields": "summary,status", "maxResults": 25, "nextPageToken": "n2"},
        "GET",
        "/rest/api/3/search/jql",
        {"jql": "project = PROJ", "fields": "summary,status", "maxResults": "25", "nextPageToken": "n2"},
        None,
    ),
    (
        "list_comments",
        {"key": "PROJ-12", "startAt": 1, "maxResults": 50, "orderBy": "created"},
        "GET",
        "/rest/api/3/issue/PROJ-12/comment",
        {"startAt": "1", "maxResults": "50", "orderBy": "created"},
        None,
    ),
    ("list_remote_links", {"key": "PROJ-12"}, "GET", "/rest/api/3/issue/PROJ-12/remotelink", {}, None),
    ("list_transitions", {"key": "PROJ-12"}, "GET", "/rest/api/3/issue/PROJ-12/transitions", {}, None),
    (
        "add_comment",
        {"key": "PROJ-12", "body": ADF_DESCRIPTION, "visibility": {"type": "role", "value": "Users"}},
        "POST",
        "/rest/api/3/issue/PROJ-12/comment",
        {},
        {"body": ADF_DESCRIPTION, "visibility": {"type": "role", "value": "Users"}},
    ),
    (
        "transition",
        {"key": "PROJ-12", "transition": {"id": "31"}, "fields": {"resolution": {"name": "Done"}}},
        "POST",
        "/rest/api/3/issue/PROJ-12/transitions",
        {},
        {"transition": {"id": "31"}, "fields": {"resolution": {"name": "Done"}}},
    ),
    (
        "create_issue",
        {"fields": {"project": {"key": "PROJ"}, "issuetype": {"name": "Task"}, "summary": "Created"}},
        "POST",
        "/rest/api/3/issue",
        {},
        {"fields": {"project": {"key": "PROJ"}, "issuetype": {"name": "Task"}, "summary": "Created"}},
    ),
]


@pytest.mark.parametrize(
    "suffix, args, method, path, query, body",
    REQUEST_CASES,
    ids=[case[0] for case in REQUEST_CASES],
)
def test_api_token_request_shape_for_every_tool(monkeypatch, suffix, args, method, path, query, body):
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", API_TOKEN)
    recorder = Recorder()
    sources = connector_entry("jira", name="work", url=SITE_BASE)["sources"]
    tool = _tool(sources, f"work_{suffix}", recorder.transport)

    assert tool.handler(args)["ok"] is True
    (request,) = recorder.requests
    assert request.method == method
    assert str(request.url).split("?", 1)[0] == SITE_BASE + path
    assert dict(request.url.params) == query
    assert (json.loads(request.content) if request.content else None) == body
    assert request.headers["authorization"] == BASIC_AUTH


@pytest.mark.parametrize(
    "suffix, args, method, path, body",
    [
        ("get_issue", {"key": "PROJ-12", "fields": "summary"}, "GET", "/rest/api/3/issue/PROJ-12", None),
        ("add_comment", {"key": "PROJ-12", "body": ADF_DESCRIPTION}, "POST", "/rest/api/3/issue/PROJ-12/comment", {"body": ADF_DESCRIPTION}),
    ],
    ids=["get-issue", "add-comment"],
)
def test_oauth_request_shape(monkeypatch, suffix, args, method, path, body):
    monkeypatch.setenv("JIRA_ACCESS_TOKEN", ACCESS_TOKEN)
    recorder = Recorder()
    sources = connector_entry("jira", name="work", url=OAUTH_BASE)["sources"]
    tool = _tool(sources, f"work_{suffix}", recorder.transport)

    assert tool.handler(args)["ok"] is True
    (request,) = recorder.requests
    assert request.method == method and str(request.url).split("?", 1)[0] == OAUTH_BASE + path
    assert (json.loads(request.content) if request.content else None) == body
    assert request.headers["authorization"] == "Bearer " + ACCESS_TOKEN


@pytest.mark.parametrize("url", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
def test_built_approval_mode_for_all_tools(url):
    expanded = connector_entry("jira", name="work", url=url)
    approvals = {
        source["name"]: build_source_tools(source)[0].manifest.approval
        for source in expanded["sources"]
    }
    assert {name for name, approval in approvals.items() if approval == "read"} == {
        "work_get_issue",
        "work_search",
        "work_list_comments",
        "work_list_remote_links",
        "work_list_transitions",
    }
    assert {name for name, approval in approvals.items() if approval == "always_approve"} == {
        "work_add_comment",
        "work_transition",
        "work_create_issue",
    }


@pytest.mark.parametrize("url", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
def test_write_approval_cannot_be_lowered_in_edited_yaml(tmp_path, url):
    entry = connector_entry("jira", name="work", url=url)
    for source in entry["sources"]:
        if source["name"].endswith(("_add_comment", "_transition", "_create_issue")):
            source["approval"] = "read"
            source["name"] = source["name"].replace("work_", "renamed_")
            source.pop("approval_mode")
            source.pop("catalog")
    entry["allow"] = [source["name"] for source in entry["sources"]]

    tools = _resolved_tools(tmp_path, entry, "oauth" if url == OAUTH_BASE else "api-token")
    assert tools["renamed_add_comment"].approval == "always_approve"
    assert tools["renamed_transition"].approval == "always_approve"
    assert tools["renamed_create_issue"].approval == "always_approve"
    assert jira.minimum("renamed_add_comment", "GET") == "always_approve"
    assert jira.minimum("renamed_get_issue", "POST") == "always_approve"


@pytest.mark.parametrize("url", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
def test_create_issue_keeps_labels_and_properties_and_approval_gates_send(monkeypatch, url):
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("JIRA_ACCESS_TOKEN", ACCESS_TOKEN)
    recorder = Recorder()
    create = next(
        source for source in connector_entry("jira", url=url)["sources"]
        if source["name"] == "jira_create_issue"
    )
    answers = iter([False, True])
    registry = ToolRegistry([], approver=lambda name, args, manifest: next(answers))
    assert register_tool_sources(registry, [create], transport=recorder.transport) == []
    args = {
        "fields": {
            "project": {"key": "PROJ"},
            "issuetype": {"name": "Task"},
            "summary": "Classify this",
            "labels": ["triage-bot", "area-billing", "needs-repro"],
        },
        "properties": [
            {"key": "automations.triage", "value": {"rules": ["security", "priority"], "score": 7}}
        ],
    }

    refused = registry.invoke("jira_create_issue", args)
    assert refused["ok"] is False and "declined" in refused["error"] and recorder.requests == []
    assert registry.invoke("jira_create_issue", args)["ok"] is True
    assert len(recorder.requests) == 1
    assert str(recorder.requests[0].url) == url + "/rest/api/3/issue"
    sent = json.loads(recorder.requests[0].content)
    assert sent["fields"]["labels"] == ["triage-bot", "area-billing", "needs-repro"]
    assert sent["properties"] == args["properties"]


@pytest.mark.parametrize("url", [OAUTH_BASE, SITE_BASE], ids=["oauth", "api-token"])
def test_paged_reads_pass_page_arguments_and_return_each_page(monkeypatch, url):
    if url == OAUTH_BASE:
        monkeypatch.setenv("JIRA_ACCESS_TOKEN", ACCESS_TOKEN)
    else:
        monkeypatch.setenv("JIRA_EMAIL", EMAIL)
        monkeypatch.setenv("JIRA_API_TOKEN", API_TOKEN)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comment"):
            body = COMMENTS_PAGE_2 if request.url.params.get("startAt") == "1" else COMMENTS_PAGE_1
        else:
            body = SEARCH_PAGE_2 if request.url.params.get("nextPageToken") == "page-two" else SEARCH_PAGE_1
        return httpx.Response(200, json=body)

    recorder = Recorder(respond)
    sources = connector_entry("jira", url=url)["sources"]
    comments = _tool(sources, "jira_list_comments", recorder.transport)
    search = _tool(sources, "jira_search", recorder.transport)

    first_comments = comments.handler({"key": "PROJ-12", "startAt": 0, "maxResults": 1})
    second_comments = comments.handler({"key": "PROJ-12", "startAt": 1, "maxResults": 1})
    first_search = search.handler({"jql": "project = PROJ", "fields": "summary", "maxResults": 1})
    second_search = search.handler(
        {"jql": "project = PROJ", "fields": "summary", "maxResults": 1, "nextPageToken": "page-two"}
    )

    assert first_comments["result"]["data"] == COMMENTS_PAGE_1
    assert second_comments["result"]["data"] == COMMENTS_PAGE_2
    assert first_search["result"]["data"] == SEARCH_PAGE_1
    assert second_search["result"]["data"] == SEARCH_PAGE_2
    assert dict(recorder.requests[0].url.params) == {"startAt": "0", "maxResults": "1"}
    assert dict(recorder.requests[1].url.params) == {"startAt": "1", "maxResults": "1"}
    assert dict(recorder.requests[3].url.params)["nextPageToken"] == "page-two"


def test_identity_returns_the_only_jira_site(monkeypatch):
    recorder = Recorder(lambda request: httpx.Response(200, json=ACCESSIBLE_RESOURCES))
    monkeypatch.setattr(connections, "_transport", recorder.transport)

    assert jira.identity({"access_token": ACCESS_TOKEN}, lambda url, headers: {}) == (CLOUD_ID, "Acme Jira")
    (request,) = recorder.requests
    assert str(request.url) == jira.ACCESSIBLE_RESOURCES_URL
    assert request.headers["authorization"] == "Bearer " + ACCESS_TOKEN
    assert request.headers["accept"] == "application/json"


@pytest.mark.parametrize(
    "resources, match",
    [
        ([], "reaches no Jira site"),
        (
            [{"id": "confluence", "name": "Docs", "url": SITE_BASE, "scopes": ["read:confluence-content.all"]}],
            "reaches no Jira site",
        ),
        (
            [
                *ACCESSIBLE_RESOURCES,
                {
                    "id": "cloud-456",
                    "name": "Other Jira",
                    "url": "https://other.atlassian.net",
                    "scopes": ["read:jira-work"],
                },
            ],
            "reaches 2 Jira sites.*acme.atlassian.net.*other.atlassian.net",
        ),
    ],
    ids=["zero", "confluence-only", "two-sites"],
)
def test_identity_refuses_zero_or_multiple_jira_sites(monkeypatch, resources, match):
    recorder = Recorder(lambda request: httpx.Response(200, json=resources))
    monkeypatch.setattr(connections, "_transport", recorder.transport)
    with pytest.raises(ConnectorError, match=match):
        jira.identity({"access_token": ACCESS_TOKEN}, lambda url, headers: {})


def test_identity_http_error_never_echoes_token(monkeypatch):
    recorder = Recorder(lambda request: httpx.Response(503, json={"echo": ACCESS_TOKEN}))
    monkeypatch.setattr(connections, "_transport", recorder.transport)
    with pytest.raises(ConnectorError, match="HTTP 503") as caught:
        jira.identity({"access_token": ACCESS_TOKEN}, lambda url, headers: {})
    assert ACCESS_TOKEN not in str(caught.value)


def test_jira_refresh_posts_expected_form_and_store_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLIENT_ID", "jira-client")
    monkeypatch.setenv("JIRA_CLIENT_SECRET", "jira-client-secret")
    responses = iter(
        [
            httpx.Response(200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "access-3", "refresh_token": "refresh-3", "expires_in": 3600}),
        ]
    )
    recorder = Recorder(lambda request: next(responses))
    provider = OAuthCodeProvider(CONNECTORS["jira"], transport=recorder.transport)

    assert provider.refresh("refresh-1")["refresh_token"] == "refresh-2"
    assert str(recorder.requests[0].url) == "https://auth.atlassian.com/oauth/token"
    assert _form(recorder.requests[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "jira-client",
        "client_secret": "jira-client-secret",
    }

    store = ConnectionStore(tmp_path / "connections.db", refreshers={"jira": provider.refresh})
    store.upsert(
        provider="jira",
        handle="main",
        access_token="access-old",
        refresh_token="refresh-old",
        expires_in=60,
        now=2_000_000.0,
    )
    assert store.get_token("jira", "main", now=2_000_030.0) == "access-3"
    assert _form(recorder.requests[1])["refresh_token"] == "refresh-old"
    with sqlite3.connect(store.path) as database:
        assert database.execute("SELECT refresh_token FROM connections").fetchone() == ("refresh-3",)


_LIVE_NAMES = ("JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_LIVE_ISSUE")
_LIVE_READY = os.environ.get("FABRI_LIVE_TESTS") == "1" and all(os.environ.get(name) for name in _LIVE_NAMES)


@pytest.mark.live
@pytest.mark.skipif(not _LIVE_READY, reason="set FABRI_LIVE_TESTS=1 and the Jira live test variables")
def test_live_jira_read_only_smoke():
    site_url = os.environ["JIRA_SITE_URL"]
    issue_key = os.environ["JIRA_LIVE_ISSUE"]
    sources = connector_entry("jira", url=site_url)["sources"]
    get_issue = _tool(sources, "jira_get_issue", httpx.HTTPTransport())
    search = _tool(sources, "jira_search", httpx.HTTPTransport())

    assert get_issue.handler({"key": issue_key, "fields": "summary"})["ok"] is True
    assert search.handler({"jql": f'key = "{issue_key}"', "fields": "summary", "maxResults": 1})["ok"] is True


@pytest.mark.parametrize(
    "key, sent",
    [("PROJ-12", 1), ("proj-12", 1), ("10042", 1), ("PROJ-1/worklog", 0), ("PROJ-1%2Fworklog", 0), ("..", 0)],
    ids=["upper-key", "lower-key", "numeric-id", "slash", "encoded-slash", "dot-segment"],
)
def test_read_key_cannot_leave_its_path_segment(monkeypatch, key, sent):
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", API_TOKEN)
    recorder = Recorder()
    tool = _tool(connector_entry("jira", name="work", url=SITE_BASE)["sources"], "work_get_issue", recorder.transport)

    result = tool.handler({"key": key})
    assert len(recorder.requests) == sent
    assert result["ok"] is bool(sent)


def test_api_token_mode_refuses_a_stored_workspace():
    with pytest.raises(ConnectorError, match="workspace only with the OAuth base"):
        connector_entry("jira", workspace="main", url=SITE_BASE)


def test_confluence_path_on_the_oauth_host_is_always_approve():
    url = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/rest/api/content"
    assert request_floor(url, "GET") == ("jira", "always_approve")


def test_identity_counts_a_site_listed_twice_once(monkeypatch):
    site = {"id": CLOUD_ID, "name": "Acme", "url": SITE_BASE, "scopes": ["read:jira-work"]}
    monkeypatch.setattr(
        connections, "_transport", httpx.MockTransport(lambda request: httpx.Response(200, json=[site, site]))
    )
    assert jira.identity({"access_token": ACCESS_TOKEN}, lambda url, headers: {}) == (CLOUD_ID, "Acme")
