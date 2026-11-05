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
