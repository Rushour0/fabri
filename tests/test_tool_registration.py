"""fabri.tools.registration: tools registered into a layer at runtime."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from fabri.setups import resolve_tools
from fabri.tools.policy import PolicyError, ToolPolicy
from fabri.tools.registration import (
    RegistrationError,
    list_registered,
    read_audit,
    register_tool,
    set_enabled,
    unregister_tool,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
POLICY = ToolPolicy(allow=("crm_*", "pet_*", "createPet", "deletePet", "listPets"),
                    allowed_kinds=("http", "openapi"), allowed_hosts=("api.crm.test", "petstore.test"))


def http_decl(**over):
    decl = {
        "name": "crm_lookup",
        "description": "Look up a contact by id.",
        "kind": "http",
        "endpoint": "https://api.crm.test/contacts/{contact_id}",
        "method": "GET",
        "input_schema": {"type": "object", "properties": {"contact_id": {"type": "string"}},
                         "required": ["contact_id"]},
        "auth": {"secret_ref": "crm:prod", "header": "Authorization", "prefix": "Bearer "},
        "approval": "read",
        "version": 1,
    }
    decl.update(over)
    return decl


def recorder(calls):
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    return httpx.MockTransport(handle)


@pytest.fixture
def layers(tmp_path):
    base, project = tmp_path / "base", tmp_path / "project"
    base.mkdir()
    project.mkdir()
    return [("base", base), ("project", project)]


def test_register_http_tool_is_callable_on_next_resolve(layers, monkeypatch):
    monkeypatch.setenv("FABRI_CRED_CRM_PROD", "token-value-123")
    project = layers[1][1]
    calls: list[httpx.Request] = []
    transport = recorder(calls)
    assert resolve_tools(layers, transport=transport).names() == []
    tool = register_tool(project, http_decl(), actor="alice", policy=POLICY)
    assert tool.path == project / "tools/registered/crm_lookup.json"
    tools = resolve_tools(layers, transport=transport)
    assert tools.names() == ["crm_lookup"]
    resolved = tools.get("crm_lookup")
    assert resolved.origin == "registered" and resolved.layer == "project" and resolved.approval == "read"
    out = tools.registry().invoke("crm_lookup", {"contact_id": "c-9"})
    assert out["ok"] and out["result"]["data"] == {"id": "c-9"}
    assert calls[-1].headers["Authorization"] == "Bearer token-value-123"
    stored = (project / "tools/registered/crm_lookup.json").read_text()
    assert "token-value-123" not in stored
    # the global cap still applies to registered tools
    assert resolve_tools(layers, global_allow=["other"], transport=transport).names() == []


@pytest.mark.parametrize("auth", [
    {"secret_ref": "sk-live-ABCdef1234567890XYZabc"},
    {"header_env": "sk_live_ABCdef1234567890XYZabc"},
    {"header_env": "my token"},
    {"value": "Bearer abc"},
    {"secret_ref": "crm:ghp_ABCdef1234567890XYZabc"},
    {"secret_ref": "crm:prod", "prefix": "Bearer ABCdef1234567890XYZabc0"},
])
def test_raw_secret_rejected(layers, auth):
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], http_decl(auth=auth), actor="alice", policy=POLICY)


def test_secret_in_url_rejected(layers):
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], http_decl(endpoint="https://api.crm.test/c?api_key=abc"), actor="a", policy=POLICY)
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], http_decl(endpoint="https://user:pw@api.crm.test/c"), actor="a", policy=POLICY)


def test_host_outside_policy_rejected(layers):
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], http_decl(endpoint="https://evil.test/x"), actor="a", policy=POLICY)


def test_delete_blocked_unless_policy_allows(layers, tmp_path):
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], http_decl(method="DELETE"), actor="a", policy=POLICY)
    shutil.copy(FIXTURES / "petstore.yaml", tmp_path / "petstore.yaml")
    openapi = {"name": "pet_admin", "kind": "openapi", "description": "Pet admin.",
               "source": {"spec": str(tmp_path / "petstore.yaml"), "operation_ids": ["deletePet"]}}
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], openapi, actor="a", policy=POLICY)
    allowing = ToolPolicy(allow=POLICY.allow, allowed_kinds=POLICY.allowed_kinds,
                          allowed_hosts=POLICY.allowed_hosts, allow_delete=True)
    register_tool(layers[1][1], openapi, actor="a", policy=allowing)
    tools = resolve_tools(layers, transport=recorder([]))
    assert tools.names() == ["pet_delete"] and tools.get("deletePet").approval == "always_approve"


def test_other_policy_rules(layers):
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], http_decl(name="other_tool"), actor="a", policy=POLICY)
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], {"name": "crm_py", "kind": "python", "description": "x",
                                     "callable": "os:system"}, actor="a", policy=POLICY)
    py_policy = ToolPolicy(allow=("crm_*",), allowed_kinds=("python",), python_modules=("tests",))
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], {"name": "crm_py", "kind": "python", "description": "x",
                                     "callable": "os:system"}, actor="a", policy=py_policy)
    register_tool(layers[1][1], {"name": "crm_py", "kind": "python", "description": "x",
                                 "callable": "tests.test_tool_sources:echo_tool"}, actor="a", policy=py_policy)


@pytest.mark.parametrize("schema", [
    {"type": "array"},
    {"type": "object", "properties": []},
    {"type": "object", "properties": {"a": {"type": "strng"}}},
    {"type": "object", "properties": {}, "required": ["missing"]},
])
def test_bad_input_schema_rejected(layers, schema):
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], http_decl(input_schema=schema), actor="a", policy=POLICY)


def test_name_and_version_rules(layers):
    project = layers[1][1]
    with pytest.raises(RegistrationError):
        register_tool(project, http_decl(name="bad name"), actor="a", policy=POLICY)
    first = register_tool(project, http_decl(), actor="a", policy=POLICY)
    same = register_tool(project, http_decl(), actor="a", policy=POLICY)
    assert first.changed and not same.changed
    with pytest.raises(RegistrationError, match="bump the version"):
        register_tool(project, http_decl(description="Changed."), actor="a", policy=POLICY)
    newer = register_tool(project, http_decl(description="Changed.", version="1.1"), actor="b", policy=POLICY)
    assert newer.version == "1.1" and newer.decl["description"] == "Changed."
    with pytest.raises(RegistrationError, match="higher version"):
        register_tool(project, http_decl(version=1), actor="a", policy=POLICY)
    assert [t.name for t in list_registered(project)] == ["crm_lookup"]


def test_disable_hides_and_unregister(layers):
    project = layers[1][1]
    register_tool(project, http_decl(), actor="a", policy=POLICY)
    transport = recorder([])
    assert resolve_tools(layers, transport=transport).names() == ["crm_lookup"]
    set_enabled(project, "crm_lookup", False, actor="a")
    assert resolve_tools(layers, transport=transport).names() == []
    set_enabled(project, "crm_lookup", True, actor="a")
    assert resolve_tools(layers, transport=transport).names() == ["crm_lookup"]
    assert unregister_tool(project, "crm_lookup", actor="a") is True
    assert unregister_tool(project, "crm_lookup", actor="a") is False
    assert resolve_tools(layers, transport=transport).names() == []


def test_audit_lines(layers, monkeypatch):
    monkeypatch.setenv("FABRI_CRED_CRM_PROD", "never-in-audit")
    project = layers[1][1]
    register_tool(project, http_decl(), actor="alice", policy=POLICY)
    register_tool(project, http_decl(), actor="alice", policy=POLICY)  # no-op, no line
    register_tool(project, http_decl(version=2, description="v2"), actor="bob", policy=POLICY)
    set_enabled(project, "crm_lookup", False, actor="carol")
    unregister_tool(project, "crm_lookup", actor="dave")
    lines = read_audit(project)
    assert [(l["actor"], l["action"], l["version"]) for l in lines] == [
        ("alice", "register", "1"), ("bob", "register", "2"), ("carol", "disable", "2"), ("dave", "unregister", "2")]
    assert all(set(l) == {"ts", "actor", "action", "name", "version", "content_hash"} for l in lines)
    assert all(l["content_hash"].startswith("sha256:") and l["name"] == "crm_lookup" for l in lines)
    raw = (project / "tools/registered/_audit.jsonl").read_text()
    assert "never-in-audit" not in raw and "crm:prod" not in raw
    assert lines[1]["content_hash"] == lines[2]["content_hash"]


def test_openapi_registration_by_operation_id(layers, tmp_path):
    shutil.copy(FIXTURES / "petstore.yaml", layers[1][1] / "petstore.yaml")
    (layers[1][1] / "tools").mkdir()
    shutil.move(str(layers[1][1] / "petstore.yaml"), layers[1][1] / "tools" / "petstore.yaml")
    decl = {"name": "pets", "kind": "openapi", "description": "Pet listing and creation.",
            "source": {"spec": "petstore.yaml", "operation_ids": ["listPets", "createPet"]}}
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], {**decl, "source": {"spec": "petstore.yaml", "operation_ids": ["nope"]}},
                      actor="a", policy=POLICY)
    tool = register_tool(layers[1][1], decl, actor="a", policy=POLICY)
    assert tool.approval == "write"
    tools = resolve_tools(layers, transport=recorder([]))
    assert set(tools.names()) == {"pet_list", "pet_create"}
    assert json.loads((layers[1][1] / "tools/registered/pets.json").read_text())["decl"]["source"]["operation_ids"] == ["listPets", "createPet"]


# ------------------------------------------------------------ mcp server.env

MCP_POLICY = ToolPolicy(allow=("mcp_*",), allowed_kinds=("mcp",), allowed_hosts=None)

_MCP_CHILD = """
import json, os, sys
var = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05"}}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo", "description": "echo",
               "inputSchema": {"type": "object", "properties": {"v": {"type": "string"}}}}]}}
    else:
        out = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "ok"}],
               "structuredContent": {"declared": os.environ.get("DECLARED_KEY", ""),
                                     "parent": os.environ.get("PARENT_ONLY", "")}}}
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
"""


def mcp_decl(env=None, **server_over):
    import sys

    server = {"command": [sys.executable, "-c", _MCP_CHILD, "x"], **server_over}
    if env is not None:
        server["env"] = env
    return {"name": "echo", "description": "Echo server.", "kind": "mcp", "server": server, "version": 1}


def test_mcp_env_refs_accepted_and_hashed(layers):
    project = layers[1][1]
    a = register_tool(project, mcp_decl({"DECLARED_KEY": {"secret_ref": "openai:prod"}}), actor="a", policy=MCP_POLICY)
    b = register_tool(project, mcp_decl({"DECLARED_KEY": {"env": "HOST_KEY"}}, ) | {"version": 2}, actor="a",
                      policy=MCP_POLICY)
    assert a.content_hash != b.content_hash
    assert a.decl["server"]["env"] == {"DECLARED_KEY": {"secret_ref": "openai:prod"}}


@pytest.mark.parametrize("env", [
    {"K": "sk-live-ABCdef1234567890XYZabc"},
    {"K": {"value": "hunter2"}},
    {"K": {"secret_ref": "sk-live-ABCdef1234567890XYZabc"}},
    {"K": {"secret_ref": "openai:ghp_ABCdef1234567890XYZabc"}},
    {"K": {"env": "sk_live_ABCdef1234567890XYZabc"}},
    {"K": {"env": "has space"}},
    {"bad name": {"env": "HOST_KEY"}},
    {"K": {"env": "A", "secret_ref": "a:b"}},
    {},
])
def test_mcp_env_literals_rejected(layers, env):
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], mcp_decl(env), actor="a", policy=MCP_POLICY)


def test_mcp_env_mode_inherit_rejected(layers):
    decl = mcp_decl({"K": {"env": "HOST_KEY"}}, env_mode="inherit")
    with pytest.raises(RegistrationError, match="inherit"):
        register_tool(layers[1][1], decl, actor="a", policy=MCP_POLICY)


def test_mcp_env_checked_against_credential_grants(layers):
    policy = ToolPolicy(allow=("mcp_*",), allowed_kinds=("mcp",), allowed_hosts=None,
                        credential_grants=(("echo", ("other:*",)),))
    with pytest.raises(PolicyError):
        register_tool(layers[1][1], mcp_decl({"K": {"secret_ref": "openai:prod"}}), actor="a", policy=policy)


def test_mcp_env_on_http_server_rejected(layers):
    decl = {"name": "echo", "description": "d", "kind": "mcp", "version": 1,
            "server": {"url": "https://x.test/mcp", "env": {"K": {"env": "A"}}}}
    with pytest.raises(RegistrationError):
        register_tool(layers[1][1], decl, actor="a", policy=ToolPolicy.permissive())


def test_registered_stdio_child_gets_declared_var_only(layers, monkeypatch):
    monkeypatch.setenv("HOST_KEY", "declared-value-1")
    monkeypatch.setenv("PARENT_ONLY", "undeclared-value-2")
    project = layers[1][1]
    register_tool(project, mcp_decl({"DECLARED_KEY": {"env": "HOST_KEY"}}), actor="a", policy=MCP_POLICY)
    tools = resolve_tools(layers, policy=MCP_POLICY)
    assert tools.errors == []
    registry = tools.registry()
    try:
        out = registry.invoke("mcp_echo_echo", {"v": "x"})
        assert out["ok"], out
        blob = json.dumps(out)
        assert "undeclared-value-2" not in blob
        # the declared value comes back redacted, which proves the child had it
        assert "declared-value-1" not in blob and "[redacted]" in blob
        raw = (project / "tools/registered/echo.json").read_text() + (project / "tools/registered/_audit.jsonl").read_text()
        assert "declared-value-1" not in raw and "undeclared-value-2" not in raw
    finally:
        for client in getattr(registry, "_mcp_clients", []) or []:
            client.close()


def test_mcp_env_audit_has_no_value(layers, monkeypatch):
    monkeypatch.setenv("HOST_KEY", "secret-in-host-9")
    project = layers[1][1]
    register_tool(project, mcp_decl({"DECLARED_KEY": {"env": "HOST_KEY"}}), actor="a", policy=MCP_POLICY)
    assert "secret-in-host-9" not in (project / "tools/registered/_audit.jsonl").read_text()
    assert set(read_audit(project)[0]) == {"ts", "actor", "action", "name", "version", "content_hash"}


def test_cli_env_flag_builds_refs():
    from fabri.setups.cli import _add_env_flags

    decl = {"server": {"command": ["x"]}}
    _add_env_flags(decl, ["A=openai:prod", "B=HOST_B"])
    assert decl["server"]["env"] == {"A": {"secret_ref": "openai:prod"}, "B": {"env": "HOST_B"}}
    with pytest.raises(SystemExit):
        _add_env_flags({"server": {"command": ["x"]}}, ["noequals"])
