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
