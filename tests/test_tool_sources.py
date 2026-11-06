"""fabri.tools.sources and fabri.tools.policy: one declaration format for
openapi, http, mcp and python tools."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fabri.tools.policy import PolicyError, ToolPolicy
from fabri.tools.registry import ToolRegistry
from fabri.tools.sources import (
    ToolSourceError,
    build_source_tools,
    build_tool_source,
    register_tool_sources,
    validate_source_decl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def echo_tool(args: dict) -> dict:
    """Echo the arguments back."""
    return {"echo": args}


def _transport(calls):
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"seen": request.url.path})

    return httpx.MockTransport(handle)


def test_openapi_source_relative_spec(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "pets.yaml").write_text((FIXTURES / "petstore.yaml").read_text())
    pairs = build_tool_source({"kind": "openapi", "name": "pets", "spec": "specs/pets.yaml", "methods": ["GET"]},
                              base_dir=tmp_path, transport=_transport([]))
    names = {m.name for m, _ in pairs}
    assert {"pet_list", "pet_get", "owner_list"} <= names and len(names) == 4
    assert all(m.approval == "read" for m, _ in pairs)


def test_http_source_path_params_query_and_body():
    calls: list[httpx.Request] = []
    get_tool = build_source_tools({
        "kind": "http", "name": "weather_now", "url": "https://weather.test/now/{city}", "method": "GET",
        "description": "Current weather.", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }, transport=_transport(calls))[0]
    assert get_tool.manifest.approval == "read"
    out = get_tool.handler({"city": "New York", "units": "c"})
    assert out["ok"] and calls[-1].url.path == "/now/New York" and calls[-1].url.params["units"] == "c"
    post_tool = build_source_tools({"kind": "http", "name": "note_add", "url": "https://notes.test/notes",
                                    "method": "POST"}, transport=_transport(calls))[0]
    assert post_tool.manifest.approval == "write"
    post_tool.handler({"text": "hi"})
    assert json.loads(calls[-1].content) == {"text": "hi"}


def test_python_source():
    tool = build_source_tools({"kind": "python", "name": "echo", "callable": "tests.test_tool_sources:echo_tool"})[0]
    assert tool.manifest.description == "Echo the arguments back."
    assert tool.handler({"a": 1}) == {"ok": True, "result": {"status": "ok", "data": {"echo": {"a": 1}}}}


def test_mcp_source_uses_existing_client(monkeypatch):
    class FakeClient:
        pass

    from fabri.tools import mcp_client
    from fabri.tools.manifest_schema import ToolManifest

    def fake_build(cfg, approval_for=None):
        assert cfg["name"] == "files" and cfg["command"] == ["mcp-files"]
        m = ToolManifest(name="mcp_files_read", description="read", command=[], input_schema={}, output_schema={})
        return FakeClient(), [(m, lambda args: {"ok": True})]

    monkeypatch.setattr(mcp_client, "build_mcp_tools", fake_build)
    tools = build_source_tools({"kind": "mcp", "name": "files", "command": ["mcp-files"], "approval": "read"})
    assert tools[0].manifest.name == "mcp_files_read" and tools[0].manifest.approval == "read"


@pytest.mark.parametrize(
    "decl,error",
    [
        ({"kind": "ftp", "name": "x"}, ToolSourceError),
        ({"kind": "http", "name": "bad name"}, ToolSourceError),
        ({"kind": "http", "name": "x"}, ToolSourceError),
        ({"kind": "http", "name": "x", "url": "https://a.test", "input_schema": {"type": "array"}}, ToolSourceError),
        ({"kind": "http", "name": "x", "url": "https://a.test", "approval": "maybe"}, ValueError),
        ({"kind": "mcp", "name": "x"}, ToolSourceError),
        ({"kind": "python", "name": "x", "callable": "os.system; rm"}, ToolSourceError),
        ({"kind": "http", "name": "x", "url": "https://a.test", "headers": {"A": {"env": "a b"}}}, ToolSourceError),
    ],
)
def test_invalid_decls(decl, error):
    with pytest.raises(error):
        validate_source_decl(decl)


def test_unknown_keys_warn():
    assert validate_source_decl({"kind": "http", "name": "x", "url": "https://a.test", "colour": 1}) == [
        "source x: unknown keys ['colour']"]


def test_policy_checks():
    strict = ToolPolicy(allowed_hosts=("*.allowed.test",))
    with pytest.raises(PolicyError):
        validate_source_decl({"kind": "python", "name": "x", "callable": "a.b"}, strict)
    with pytest.raises(PolicyError):
        validate_source_decl({"kind": "http", "name": "x", "url": "https://evil.test/a"}, strict)
    validate_source_decl({"kind": "http", "name": "x", "url": "https://api.allowed.test/a"}, strict)
    with pytest.raises(PolicyError):
        validate_source_decl({"kind": "http", "name": "x", "url": "https://api.allowed.test/a", "method": "DELETE"}, strict)
    mods = ToolPolicy(allowed_kinds=("python",), python_modules=("tests",))
    validate_source_decl({"kind": "python", "name": "x", "callable": "tests.test_tool_sources:echo_tool"}, mods)
    with pytest.raises(PolicyError):
        validate_source_decl({"kind": "python", "name": "x", "callable": "testsx.mod:f"}, mods)


def test_policy_from_data():
    assert ToolPolicy.from_data(["a", "b_*"]).allow == ("a", "b_*")
    assert ToolPolicy.from_data("a\n# c\nb\n").allow == ("a", "b")
    pol = ToolPolicy.from_data({"allow": ["x"], "allowed_kinds": ["http"], "allow_delete": True})
    assert pol.allowed_kinds == ("http",) and pol.allow_delete
    with pytest.raises(PolicyError):
        ToolPolicy.from_data({"widen": True})


def test_register_tool_sources_reports_errors():
    registry = ToolRegistry([])
    errors = register_tool_sources(registry, [
        {"kind": "python", "name": "echo", "callable": "tests.test_tool_sources:echo_tool"},
        {"kind": "openapi", "name": "missing", "spec": "/nope/missing.yaml"},
    ])
    assert "echo" in registry.tools
    assert len(errors) == 1 and errors[0].startswith("missing:")


# ---------------------------------------------------- tighten clamp and catalog minimum


def test_tighten_clamp_http_and_python():
    decl = {"kind": "http", "name": "mail_send", "url": "https://a.test/send", "method": "POST", "approval": "read"}
    assert build_source_tools(decl)[0].manifest.approval == "read"
    tool = build_source_tools({**decl, "approval_mode": "tighten"})[0]
    assert tool.safe_approval == "always_approve" and tool.manifest.approval == "always_approve"
    py = build_source_tools({"kind": "python", "name": "echo", "callable": "tests.test_tool_sources:echo_tool",
                             "approval": "read", "approval_mode": "tighten"})[0]
    assert py.manifest.approval == "write"


def test_tighten_clamp_openapi_map():
    base = {"kind": "openapi", "name": "pets", "spec": str(FIXTURES / "petstore.yaml"),
            "approval": {"pet_create": "read", "pet_list": "read"}}
    loose = {t.manifest.name: t for t in build_source_tools(base, transport=_transport([]))}
    assert loose["pet_create"].manifest.approval == "read"
    tight = {t.manifest.name: t for t in build_source_tools({**base, "approval_mode": "tighten"},
                                                            transport=_transport([]))}
    assert tight["pet_create"].manifest.approval == tight["pet_create"].safe_approval != "read"
    assert tight["pet_list"].manifest.approval == "read"
    with pytest.raises(ToolSourceError):
        validate_source_decl({**base, "approval_mode": "loosen"})


def test_catalog_minimum_applied(monkeypatch):
    from fabri.tools import connectors

    seen = []

    def fake_minimum(kind, tool_name, method):
        seen.append((kind, tool_name, method))
        return "always_approve" if method == "POST" else None

    monkeypatch.setattr(connectors, "CONNECTORS", {**connectors.CONNECTORS, "fake": object()})
    monkeypatch.setattr(connectors, "catalog_minimum", fake_minimum, raising=False)
    post = build_source_tools({"kind": "http", "name": "note_add", "url": "https://a.test/n", "method": "POST",
                               "approval": "read", "catalog": "fake"})[0]
    assert post.manifest.approval == "always_approve" and post.safe_approval == "always_approve"
    get = build_source_tools({"kind": "http", "name": "note_get", "url": "https://a.test/n", "catalog": "fake"})[0]
    assert get.manifest.approval == "read" and seen == [("fake", "note_add", "POST"), ("fake", "note_get", "GET")]


def test_unknown_catalog_id_rejected():
    with pytest.raises(ToolSourceError, match="catalog"):
        validate_source_decl({"kind": "http", "name": "x", "url": "https://a.test", "catalog": "nope"})


def test_new_keys_are_known_keys():
    assert validate_source_decl({"kind": "mcp", "name": "x", "command": ["c"], "env_mode": "allowlist",
                                 "approval_mode": "tighten"}) == []
    with pytest.raises(ToolSourceError, match="env_mode"):
        validate_source_decl({"kind": "mcp", "name": "x", "command": ["c"], "env_mode": "all"})


def test_mcp_source_passes_env_mode_and_transport(monkeypatch):
    from fabri.tools import mcp_client

    got = {}

    def fake_build(cfg, approval_for=None):
        got.update(cfg)
        return object(), []

    monkeypatch.setattr(mcp_client, "build_mcp_tools", fake_build)
    transport = _transport([])
    build_source_tools({"kind": "mcp", "name": "a", "command": ["c"], "env_mode": "allowlist"}, transport=transport)
    assert got["inherit_env"] is False and got["transport"] is transport
    build_source_tools({"kind": "mcp", "name": "a", "command": ["c"]})
    assert got["inherit_env"] is True


def test_sensitive_words():
    from fabri.tools.approval import mcp_approval, safe_approval

    for name in ("scheduleCampaign", "exportContacts", "createAction", "patchSender"):
        assert safe_approval("POST", name) == "always_approve", name
    assert safe_approval("GET", "listSenders") == "read"
    assert mcp_approval("GMAIL_SEND_EMAIL") == "always_approve"
    assert mcp_approval("resume_workflow") == "always_approve"


# ---------------------------------------------------- floor bound to the request host


def test_slack_host_floor_ignores_name_method_and_markers():
    post = build_source_tools({"kind": "http", "name": "ops_note", "url": "https://slack.com/api/chat.postMessage",
                               "method": "GET", "approval": "read"})[0]
    assert post.manifest.approval == "always_approve" and post.safe_approval == "always_approve"
    read = build_source_tools({"kind": "http", "name": "ops_list",
                               "url": "https://slack.com/api/conversations.list", "method": "GET"})[0]
    assert read.manifest.approval == "read"
    # A host the caller fills in could be slack.com, so it gets the floor too.
    templated = build_source_tools({"kind": "http", "name": "ops_any", "url": "https://{host}/api/chat.postMessage",
                                    "method": "GET", "approval": "read"})[0]
    assert templated.manifest.approval == "always_approve"


def test_openapi_on_slack_host_gets_floor():
    base = {"kind": "openapi", "name": "pets", "spec": str(FIXTURES / "petstore.yaml"),
            "base_url": "https://slack.com/api", "approval": "read"}
    tools = build_source_tools(base, transport=_transport([]))
    assert tools and all(t.manifest.approval == "always_approve" for t in tools)


def test_mcp_http_source_matched_by_server_host(monkeypatch):
    from fabri.tools import connectors, mcp_client
    from fabri.tools.manifest_schema import ToolManifest

    def fake_build(cfg, approval_for=None):
        manifest = ToolManifest(name="mcp_x_save_issue", description="d", command=[],
                                input_schema={"type": "object"}, output_schema={"type": "object"},
                                approval="write")
        return object(), [(manifest, lambda args: {})]

    seen = []
    monkeypatch.setattr(mcp_client, "build_mcp_tools", fake_build)
    monkeypatch.setattr(connectors, "catalog_minimum",
                        lambda kind, name, method: seen.append(kind) or "always_approve")
    # No markers, approval lowered in the YAML: the server host still binds the linear floor.
    tool = build_source_tools({"kind": "mcp", "name": "x", "url": "https://mcp.linear.app/mcp",
                               "approval": "read"})[0]
    assert tool.manifest.approval == "always_approve" and seen == ["linear"]
    seen.clear()
    other = build_source_tools({"kind": "mcp", "name": "x", "url": "https://mcp.other.test/mcp",
                                "approval": "read"})[0]
    assert other.manifest.approval == "read" and seen == []


# ---------------------------------------------------- 401 hint for stored credentials


def _refusing_transport(seen):
    def handle(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(401, json={"detail": "invalid token"})

    return httpx.MockTransport(handle)


def test_http_401_with_secret_ref_names_provider_and_fabri_connect(caplog):
    token = "pat_live_0123456789abcdef"

    class Store:
        def get(self, provider, handle):
            return token

    seen: list = []
    tool = build_source_tools({"kind": "http", "name": "ck_lists", "url": "https://api.example.test/lists",
                               "method": "GET",
                               "headers": {"Authorization": {"secret_ref": "cakemail:main", "prefix": "Bearer "}}},
                              transport=_refusing_transport(seen), store=Store())[0]
    out = tool.handler({})
    assert seen == [f"Bearer {token}"]
    assert out["ok"] is False
    assert out["error"] == "HTTP 401; the stored cakemail credential was refused, run fabri connect cakemail"
    assert out["result"]["code"] == 401 and out["result"]["status"] == "error"
    assert token not in out["error"] and token not in caplog.text


def test_http_401_with_env_header_keeps_plain_message(monkeypatch):
    monkeypatch.setenv("ACME_TOKEN", "env_token_value_123")
    seen: list = []
    tool = build_source_tools({"kind": "http", "name": "acme_get", "url": "https://api.example.test/things",
                               "method": "GET", "headers": {"Authorization": {"env": "ACME_TOKEN"}}},
                              transport=_refusing_transport(seen))[0]
    out = tool.handler({})
    assert seen == ["env_token_value_123"]
    assert out["ok"] is False and out["error"] == "HTTP 401"


# ---------------------------------------------------- credential broker routing


from fabri.tools.credential_broker_client import BrokerClient  # noqa: E402

CK_HEADERS = {"Authorization": {"secret_ref": "cakemail:main", "prefix": "Bearer "}}


class FakeBroker(BrokerClient):
    """A broker client that records what it would send and never opens a
    socket. `sender` is the real one, so the keyword shape is the real one."""

    def __init__(self):
        super().__init__("/nonexistent/brk.sock", "test-capability", approval_timeout_s=1.0)
        self.sent: list[dict] = []
        self.posted: list[dict] = []

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return {"v": 1, "ok": True, "result": {"status": "ok", "code": 200, "data": {"via": "broker"}}}

    def post_mcp(self, **kwargs):
        self.posted.append(kwargs)
        return 200, {"content-type": "application/json"}, b"{}"


def _no_network(request):
    raise AssertionError(f"a brokered tool sent in process: {request.method} {request.url}")


def test_http_source_with_secret_ref_uses_broker_sender_and_marks_gate():
    broker = FakeBroker()
    decl = {"kind": "http", "name": "ck_contacts_create", "url": "https://api.cakemail.test/lists/{list_id}/contacts",
            "method": "POST", "headers": CK_HEADERS, "approval": "always_approve",
            "dry_run_header": {"name": "X-Dry-Run", "value": "true"}}
    tool = build_source_tools(decl, broker=broker, dry_run=True,
                              transport=httpx.MockTransport(_no_network))[0]
    assert tool.broker_gated is True and tool.handler.approval_gate == "broker"
    out = tool.handler({"list_id": 42, "email": "a@b.test"})
    assert out["ok"] is True and out["result"]["data"] == {"via": "broker"}
    sent = broker.sent[-1]
    assert sent["source"] == "ck_contacts_create" and sent["tool"] == "ck_contacts_create"
    assert sent["method"] == "POST"
    # Raw values; the broker renders the URL from its own template.
    assert sent["path_params"] == {"list_id": 42}
    assert sent["url"] == "https://api.cakemail.test/lists/42/contacts"
    assert sent["floor_url"] == decl["url"]
    assert sent["json_body"] == {"email": "a@b.test"} and sent["params"] is None
    assert sent["extra_headers"] == {"X-Dry-Run": "true"}
    # A bad path value is refused before anything reaches the broker.
    count = len(broker.sent)
    assert tool.handler({"list_id": "../x", "email": "a@b.test"})["ok"] is False
    assert len(broker.sent) == count

    get_tool = build_source_tools({"kind": "http", "name": "ck_lists", "url": "https://api.cakemail.test/lists",
                                   "method": "GET", "headers": CK_HEADERS}, broker=broker)[0]
    get_tool.handler({"page": 2})
    assert broker.sent[-1]["params"] == {"page": 2} and broker.sent[-1]["json_body"] is None

    # Registered with a live broker: marked, so the registry does not ask a
    # second time and the always_approve call reaches the broker.
    registry = ToolRegistry([])
    assert register_tool_sources(registry, [decl], broker=broker) == []
    assert registry._broker_gated("ck_contacts_create") is True
    out = registry.invoke("ck_contacts_create", {"list_id": 7, "email": "c@d.test"})
    assert out["ok"] is True and broker.sent[-1]["path_params"] == {"list_id": 7}

    # No broker client: nothing is marked and the normal approver gate holds.
    plain = ToolRegistry([])
    assert register_tool_sources(plain, [decl], transport=httpx.MockTransport(_no_network)) == []
    assert plain._broker_gated("ck_contacts_create") is False
    refused = plain.invoke("ck_contacts_create", {"list_id": 7, "email": "c@d.test"})
    assert refused["ok"] is False and "needs human approval" in refused["error"]


def test_http_source_with_env_header_stays_in_process(monkeypatch):
    monkeypatch.setenv("ACME_TOKEN", "env_token_value_123")
    broker = FakeBroker()
    calls: list[httpx.Request] = []
    decl = {"kind": "http", "name": "acme_get", "url": "https://api.example.test/things/{id}", "method": "GET",
            "headers": {"Authorization": {"env": "ACME_TOKEN"}, "X-Client": "fabri"}}
    tool = build_source_tools(decl, broker=broker, transport=_transport(calls))[0]
    assert tool.broker_gated is False and getattr(tool.handler, "approval_gate", None) is None
    assert tool.handler({"id": 5})["ok"] is True
    assert broker.sent == [] and calls[-1].url.path == "/things/5"
    assert calls[-1].headers["Authorization"] == "env_token_value_123"
    registry = ToolRegistry([])
    assert register_tool_sources(registry, [decl], broker=broker, transport=_transport(calls)) == []
    assert "acme_get" in registry.tools and registry._broker_gated("acme_get") is False


def test_openapi_source_routes_every_op_through_broker():
    broker = FakeBroker()
    decl = {"kind": "openapi", "name": "pets", "spec": str(FIXTURES / "petstore.yaml"),
            "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"], "headers": CK_HEADERS,
            "dry_run_header": {"name": "X-Dry-Run", "value": "1"}}
    tools = build_source_tools(decl, broker=broker, dry_run=True, transport=httpx.MockTransport(_no_network))
    assert len(tools) > 4
    assert all(t.broker_gated and t.handler.approval_gate == "broker" for t in tools)
    by_name = {t.manifest.name: t for t in tools}
    by_name["pet_get"].handler({"petId": 3})
    sent = broker.sent[-1]
    assert sent["source"] == "pets" and sent["tool"] == "pet_get" and sent["method"] == "GET"
    assert sent["path_params"] == {"petId": 3} and sent["extra_headers"] == {"X-Dry-Run": "1"}
    by_name["pet_adopt"].handler({"petId": 4, "owner_id": 9})
    sent = broker.sent[-1]
    assert sent["tool"] == "pet_adopt" and sent["method"] == "POST"
    assert sent["path_params"] == {"petId": 4} and sent["json_body"] == {"owner_id": 9}
    # Each op has its own sender bound to its own tool name.
    for tool in tools:
        if not any(p.location == "path" for p in tool.operation.params):
            tool.handler({})
            assert broker.sent[-1]["tool"] == tool.manifest.name
    registry = ToolRegistry([])
    assert register_tool_sources(registry, [decl], broker=broker) == []
    assert all(registry._broker_gated(name) for name in by_name)


def test_mcp_url_source_gets_poster_and_no_headers(monkeypatch):
    from fabri.tools import mcp_client
    from fabri.tools.manifest_schema import ToolManifest

    seen: list[dict] = []

    def fake_build(cfg, approval_for=None):
        seen.append(dict(cfg))
        m = ToolManifest(name="mcp_gh_list", description="d", command=[], input_schema={"type": "object"},
                         output_schema={"type": "object"}, approval="write")
        return object(), [(m, lambda args: {"ok": True})]

    monkeypatch.setattr(mcp_client, "build_mcp_tools", fake_build)
    broker = FakeBroker()
    decl = {"kind": "mcp", "name": "gh", "url": "https://mcp.example.test/mcp", "headers": CK_HEADERS,
            "dry_run_header": {"name": "X-Dry-Run", "value": "1"}}
    tools = build_source_tools(decl, broker=broker, dry_run=True)
    cfg = seen[-1]
    assert "headers" not in cfg and "header_source" not in cfg
    assert cfg["url"] == decl["url"] and callable(cfg["poster"])
    assert "cakemail" in cfg["credential_hint"]
    status, headers, body = cfg["poster"](body=b'{"jsonrpc":"2.0"}', headers={"Mcp-Session-Id": "abc"},
                                          timeout_s=5.0)
    assert (status, body) == (200, b"{}")
    posted = broker.posted[-1]
    assert posted["source"] == "gh" and posted["url"] == decl["url"] and posted["timeout_s"] == 5.0
    assert posted["body"] == b'{"jsonrpc":"2.0"}'
    assert posted["headers"] == {"Mcp-Session-Id": "abc", "X-Dry-Run": "1"}
    assert all(t.broker_gated and t.handler.approval_gate == "broker" for t in tools)
    registry = ToolRegistry([])
    assert register_tool_sources(registry, [decl], broker=broker) == []
    assert registry._broker_gated("mcp_gh_list") is True

    # Without a broker the same declaration resolves headers in process.
    class Store:
        def get(self, provider, handle):
            return "store-value"

    build_source_tools(decl, store=Store())
    assert "poster" not in seen[-1] and "header_source" in seen[-1]


def test_stdio_mcp_with_ref_refused_under_broker(monkeypatch):
    from fabri.tools import mcp_client

    def fake_build(cfg, approval_for=None):
        raise AssertionError("a refused source must not start its server")

    monkeypatch.setattr(mcp_client, "build_mcp_tools", fake_build)
    decl = {"kind": "mcp", "name": "files", "command": ["mcp-files"],
            "env": {"FILES_TOKEN": {"secret_ref": "files:main"}}}
    with pytest.raises(ToolSourceError) as err:
        build_source_tools(decl, broker=FakeBroker())
    assert str(err.value) == ("source files: a stdio MCP server cannot use a stored credential under fabri serve; "
                              "use the http transport, or set FABRI_CRED_SNAPSHOT=1 on the service")
    registry = ToolRegistry([])
    errors = register_tool_sources(registry, [decl], broker=FakeBroker())
    assert errors == ["files: source files: a stdio MCP server cannot use a stored credential under fabri "
                      "serve; use the http transport, or set FABRI_CRED_SNAPSHOT=1 on the service"]
    assert registry.tools == {}


def test_python_source_with_headers_refused_under_broker():
    decl = {"kind": "python", "name": "echo", "callable": "tests.test_tool_sources:echo_tool",
            "headers": CK_HEADERS}
    with pytest.raises(ToolSourceError, match="python source cannot use a stored credential"):
        build_source_tools(decl, broker=FakeBroker())
    # A python source with no ref still builds with a broker present.
    plain = build_source_tools({"kind": "python", "name": "echo", "callable": "tests.test_tool_sources:echo_tool"},
                               broker=FakeBroker())[0]
    assert plain.broker_gated is False


def test_manifest_classes_identical_with_and_without_broker():
    decls = [
        {"kind": "openapi", "name": "pets", "spec": str(FIXTURES / "petstore.yaml"),
         "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"], "headers": CK_HEADERS},
        {"kind": "http", "name": "slack_post", "url": "https://slack.com/api/chat.postMessage", "method": "POST",
         "headers": CK_HEADERS, "approval": "read"},
        {"kind": "http", "name": "ck_lists", "url": "https://api.cakemail.test/lists", "method": "GET",
         "headers": CK_HEADERS},
    ]
    for decl in decls:
        plain = build_source_tools(decl, transport=httpx.MockTransport(_no_network))
        brokered = build_source_tools(decl, broker=FakeBroker())
        assert [(t.manifest.name, t.manifest.approval, t.safe_approval) for t in plain] == \
            [(t.manifest.name, t.manifest.approval, t.safe_approval) for t in brokered]
        assert all(t.broker_gated for t in brokered) and not any(t.broker_gated for t in plain)
    slack = build_source_tools(decls[1], broker=FakeBroker())[0]
    assert slack.manifest.approval == "always_approve"


# ---------------------------------------------------------------- query and body_format: rfc822


MAIL_DECL = {"kind": "http", "name": "mail_send", "url": "https://mail.example.test/upload/send",
             "method": "POST", "query": {"uploadType": "media"}, "body_format": "rfc822",
             "headers": {"Authorization": {"env": "MAIL_TOKEN", "prefix": "Bearer "}}}


def test_http_query_and_body_format_are_checked():
    assert validate_source_decl(MAIL_DECL) == []
    bad = [
        ({**MAIL_DECL, "body_format": "xml"}, "body_format"),
        ({**MAIL_DECL, "method": "GET"}, "POST or PUT"),
        ({**MAIL_DECL, "url": "https://mail.example.test/upload/{box}/send"}, "path parameters"),
        ({**MAIL_DECL, "query": {"uploadType": 1}}, "query"),
        ({**MAIL_DECL, "query": ["uploadType=media"]}, "query"),
    ]
    for decl, words in bad:
        with pytest.raises(ToolSourceError, match=words):
            validate_source_decl(decl)


def test_http_query_pairs_win_over_arguments():
    calls = []
    decl = {"kind": "http", "name": "search", "url": "https://api.example.test/find", "method": "GET",
            "query": {"format": "metadata"}}
    tool = build_source_tools(decl, transport=_transport(calls))[0]
    assert tool.handler({"q": "x", "format": "raw"})["ok"] is True
    assert calls[-1].url.params["format"] == "metadata" and calls[-1].url.params["q"] == "x"
    post = build_source_tools({**decl, "name": "add", "method": "POST"}, transport=_transport(calls))[0]
    assert post.handler({"name": "n"})["ok"] is True
    assert str(calls[-1].url) == "https://api.example.test/find?format=metadata"
    assert json.loads(calls[-1].content) == {"name": "n"}


def test_rfc822_source_sends_a_plain_text_message(monkeypatch):
    monkeypatch.setenv("MAIL_TOKEN", "mail-token-value-123")
    calls = []
    tool = build_source_tools(MAIL_DECL, transport=_transport(calls))[0]
    assert tool.extra == {"query": {"uploadType": "media"}, "body_format": "rfc822"}
    out = tool.handler({"to": "a@example.test", "subject": "Hi", "body": "line one\nline two"})
    assert out["ok"] is True
    (sent,) = calls
    assert str(sent.url) == "https://mail.example.test/upload/send?uploadType=media"
    assert sent.headers["content-type"] == "message/rfc822"
    assert sent.headers["authorization"] == "Bearer mail-token-value-123"
    text = sent.content.decode()
    assert "To: a@example.test\r\n" in text and "Subject: Hi\r\n" in text and "line one\r\nline two" in text
    refused = tool.handler({"to": "a@example.test", "subject": "Hi\nBcc: x@example.test", "body": "b"})
    assert refused["ok"] is False and len(calls) == 1


def test_rfc822_source_under_broker_sends_named_arguments():
    broker = FakeBroker()
    decl = {**MAIL_DECL, "headers": CK_HEADERS}
    tool = build_source_tools(decl, broker=broker, transport=httpx.MockTransport(_no_network))[0]
    assert tool.broker_gated is True
    args = {"to": "a@example.test", "subject": "Hi", "body": "b"}
    assert tool.handler(args)["ok"] is True
    sent = broker.sent[-1]
    assert sent["json_body"] == args and sent["params"] == {"uploadType": "media"}
    assert sent["path_params"] == {}
    # A bad message never reaches the broker.
    assert tool.handler({"to": "nobody", "subject": "Hi", "body": "b"})["ok"] is False
    assert len(broker.sent) == 1
