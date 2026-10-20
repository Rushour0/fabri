"""`fabri setups tools add|list|enable|disable|remove|probe|sync|connectors`,
the matching Setups API routes, and the approval defaults behind them.
Everything runs against temp layers, a stdio fake MCP server and a loopback
HTTP server. No outside network."""
from __future__ import annotations

import json
import shutil
import sys
import threading
import typing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from fabri.cli import main
from fabri.service.http_server import serve_http
from fabri.service.service import FabriService
from fabri.service.setups_routes import SetupsApi
from fabri.setups import clear_cache, lint_setup, resolve_setup, resolve_tools
from fabri.setups.tool_entries import EntryError, mcp_entry, openapi_entry
from fabri.tools.approval import mcp_approval, safe_approval
from fabri.tools.mcp_client import MCPHttpClient
from fabri.tools.openapi import parse_operations
from fabri.tools.policy import ToolPolicy

FIXTURES = Path(__file__).parent / "fixtures"
PETSTORE = FIXTURES / "openapi" / "petstore.yaml"
FAKE_MCP = FIXTURES / "mcp" / "fake_mcp_server.py"
FAKE_COMMAND = f"{sys.executable} {FAKE_MCP}"


def run(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    clear_cache()
    for name in ("FABRI_SETUPS_CONFIG", "FABRI_SETUPS_ROOT", "FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield
    clear_cache()


@pytest.fixture
def layer(tmp_path):
    path = tmp_path / "layer"
    path.mkdir()
    return path


def entry_yaml(layer: Path, name: str) -> dict:
    return yaml.safe_load((layer / "tools" / "sources" / f"{name}.yaml").read_text())


# ---------------------------------------------------------------- loopback server


class _Handler(BaseHTTPRequestHandler):
    sessions: typing.ClassVar[list[str | None]] = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body, content_type="application/json", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/openapi.yaml":
            spec = PETSTORE.read_text().replace("https://petstore.test", f"http://127.0.0.1:{self.server.server_port}")
            return self._reply(200, spec, "application/yaml")
        if self.path.startswith("/pets"):
            return self._reply(200, json.dumps([{"id": 1, "name": "Rex", "status": "available"}]))
        return self._reply(404, "{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/mcp":
            return self._reply(404, "{}")
        if "text/event-stream" not in (self.headers.get("Accept") or ""):
            return self._reply(406, "{}")
        _Handler.sessions.append(self.headers.get("Mcp-Session-Id"))
        if "id" not in body:
            return self._reply(202, b"")
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "list_issues", "inputSchema": {"type": "object"}},
                                {"name": "delete_issue", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}
        event = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}) + "\n\n"
        return self._reply(200, ": ping\n\n" + event, "text/event-stream", {"Mcp-Session-Id": "s-1"})


@pytest.fixture
def loopback():
    _Handler.sessions = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------- approval defaults


def test_safe_approval_defaults():
    assert safe_approval("GET", "payment_list") == "read"
    assert safe_approval("POST", "pet_create", "/pets") == "write"
    assert safe_approval("DELETE", "pet_delete") == "always_approve"
    assert safe_approval("POST", "chat.postMessage") == "always_approve"
    assert safe_approval("POST", "invoice_refund_create", "/invoices/{id}/refunds") == "always_approve"
    assert safe_approval("POST", "sendEmail") == "always_approve"
    assert mcp_approval("search_issues", annotations={"readOnlyHint": True}) == "write"
    assert mcp_approval("archive", annotations={"destructiveHint": True}) == "always_approve"
    assert mcp_approval("delete_issue") == "always_approve"


def test_spec_hint_tightens_but_never_loosens():
    doc = yaml.safe_load(PETSTORE.read_text())
    doc["paths"]["/pets"]["post"]["x-fabri-approval"] = "read"
    doc["paths"]["/pets/{petId}"]["delete"]["x-fabri-approval"] = "write"
    ops = {op.name: op for op in parse_operations(doc)}
    assert ops["pet_create"].approval == "write"
    assert ops["pet_delete"].approval == "always_approve"
    assert parse_operations(doc, approval={"pet_create": "read"})[1].approval in ("read", "write")
    assert {op.name: op for op in parse_operations(doc, approval={"pet_create": "read"})}["pet_create"].approval == "read"


def test_policy_floor_raises_and_lint_flags_a_looser_class(layer):
    (layer / "tools").mkdir()
    shutil.copy(PETSTORE, layer / "tools" / "petstore.yaml")
    (layer / "tools" / "tools.yaml").write_text(
        "sources:\n  - {kind: openapi, name: pets, spec: petstore.yaml, methods: [GET, POST, DELETE],"
        " approval: {pet_delete: read}}\nallow: ['*']\n")
    policy = ToolPolicy.from_data({"approval_floor": {"pet_list": "always_approve"}}, base=ToolPolicy.permissive())
    tools = resolve_tools([layer], policy=policy, use_cache=False)
    assert tools.approval_for("pet_list") == "always_approve"
    assert tools.approval_for("pet_get") == "read"
    assert tools.approval_for("pet_delete") == "read"
    findings = lint_setup(resolve_setup([layer], use_cache=False), policy=policy, tool_set=tools)
    assert any("pet_delete has approval read, looser than the default always_approve" in f.message
               for f in findings)


# ---------------------------------------------------------------- add openapi


def test_add_openapi_file_writes_yaml_and_spec_copy(monkeypatch, capsys, layer):
    code, out, err = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE),
                         "--layer", str(layer), "--name", "pets", "--auth-env", "PETS_TOKEN")
    assert code == 0, err
    assert "added tools/sources/pets.yaml (openapi)" in out
    assert "7 tools: 4 read, 3 write, 0 always_approve" in out
    data = entry_yaml(layer, "pets")
    decl = data["sources"][0]
    assert decl["spec"] == "specs/pets.yaml" and (layer / "tools" / "specs" / "pets.yaml").is_file()
    assert decl["headers"] == {"Authorization": {"env": "PETS_TOKEN", "prefix": "Bearer "}}
    assert "pet_delete" not in data["allow"] and "pet_list" in data["allow"]
    names = resolve_tools([layer], use_cache=False).names()
    assert sorted(names) == sorted(data["allow"])
    audit = [json.loads(line) for line in (layer / ".fabri-audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "add-tool-source" and audit[-1]["name"] == "pets"


def test_add_openapi_delete_is_always_approve_and_floor_applies(monkeypatch, capsys, layer):
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE),
                       "--layer", str(layer), "--name", "pets", "--allow-delete", "--include", "pet_*",
                       "--approval", "write")
    assert code == 0
    assert "7 tools: 0 read, 6 write, 1 always_approve" in out
    decl = entry_yaml(layer, "pets")["sources"][0]
    assert decl["approval"]["pet_delete"] == "always_approve" and decl["approval"]["pet_list"] == "write"
    assert decl["methods"] == ["GET", "POST", "PUT", "PATCH", "DELETE"]
    tools = resolve_tools([layer], use_cache=False)
    assert tools.approval_for("pet_delete") == "always_approve" and tools.approval_for("pet_get") == "write"


def test_add_openapi_refusals(monkeypatch, capsys, layer):
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE),
                       "--layer", str(layer), "--auth-env", "sk-live-abc123")
    assert code == 1 and "environment variable name" in err
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE),
                       "--layer", str(layer), "--include", "nothing_*")
    assert code == 1 and "no operations match" in err
    assert not (layer / "tools").exists()
    run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE), "--layer", str(layer), "--name", "pets")
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE),
                       "--layer", str(layer), "--name", "pets")
    assert code == 1 and "already exists" in out
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE),
                       "--layer", str(layer), "--name", "pets", "--replace", "--dry-run", "--json")
    assert code == 0 and json.loads(out)["written"] is False


def test_add_openapi_url_links_or_snapshots(monkeypatch, capsys, layer, loopback):
    code, out, err = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", f"{loopback}/openapi.yaml",
                         "--layer", str(layer), "--name", "live")
    assert code == 0, err
    assert entry_yaml(layer, "live")["sources"][0]["spec"] == f"{loopback}/openapi.yaml"
    assert not (layer / "tools" / "specs").exists()
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", f"{loopback}/openapi.yaml",
                       "--layer", str(layer), "--name", "snap", "--snapshot", "--include", "owner_*")
    assert code == 1 and "owner_list is already built by source live" in out
    assert not (layer / "tools" / "sources" / "snap.yaml").exists()
    other = layer.parent / "other"
    other.mkdir()
    code, _, _ = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", f"{loopback}/openapi.yaml",
                     "--layer", str(other), "--name", "snap", "--snapshot", "--include", "owner_*")
    assert code == 0 and (other / "tools" / "specs" / "snap.yaml").is_file()
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", f"{loopback}/openapi.yaml?api_key=x",
                       "--layer", str(layer))
    assert code == 1 and "query string" in err


# ---------------------------------------------------------------- add mcp


def test_add_mcp_stdio_lists_tools_and_classifies(monkeypatch, capsys, layer):
    monkeypatch.setenv("FAKE_TOKEN", "t")
    code, out, err = run(monkeypatch, capsys, "setups", "tools", "add", "mcp", "tracker", "--command", FAKE_COMMAND,
                         "--env", "FAKE_TOKEN", "--layer", str(layer))
    assert code == 0, err
    assert "4 tools: 0 read, 2 write, 2 always_approve" in out
    data = entry_yaml(layer, "tracker")
    decl = data["sources"][0]
    assert decl["env"] == {"FAKE_TOKEN": {"env": "FAKE_TOKEN"}}
    assert decl["approval"] == {"mcp_tracker_archive_issue": "always_approve",
                                "mcp_tracker_send_message": "always_approve"}
    assert sorted(data["allow"]) == ["mcp_tracker_archive_issue", "mcp_tracker_create_issue",
                                     "mcp_tracker_search_issues", "mcp_tracker_send_message"]
    tools = resolve_tools([layer], use_cache=False)
    assert tools.approval_for("mcp_tracker_search_issues") == "write"
    result = tools.get("mcp_tracker_create_issue").handler({"title": "x"})
    assert result["ok"] and '"token_set": true' in result["result"]["content"][0]["text"]


def test_add_mcp_include_no_connect_and_refusals(monkeypatch, capsys, layer):
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "mcp", "files", "--command", "mcp-files /srv",
                       "--no-connect", "--include", "read_*", "--layer", str(layer))
    assert code == 0 and "tools are listed when the server is reached" in out
    assert entry_yaml(layer, "files")["allow"] == ["mcp_files_read_*"]
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "add", "mcp", "bad", "--command",
                       "srv --token=ghp_abcdefghijklmnopqrstu1234", "--no-connect", "--layer", str(layer))
    assert code == 1 and "looks like a credential" in err
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "add", "mcp", "bad", "--url", "https://a.test/mcp",
                       "--command", "x", "--layer", str(layer))
    assert code == 1 and "exactly one" in err
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "mcp", "dead", "--command",
                       f"{sys.executable} -c pass", "--layer", str(layer))
    assert code == 1 and "not added" in out and not (layer / "tools" / "sources" / "dead.yaml").exists()


def test_add_mcp_streamable_http(monkeypatch, capsys, layer, loopback):
    monkeypatch.setenv("TRACKER_TOKEN", "t")
    code, out, err = run(monkeypatch, capsys, "setups", "tools", "add", "mcp", "remote", "--url", f"{loopback}/mcp",
                         "--auth-env", "TRACKER_TOKEN", "--layer", str(layer))
    assert code == 0, err
    assert "2 tools: 0 read, 1 write, 1 always_approve" in out
    decl = entry_yaml(layer, "remote")["sources"][0]
    assert decl["headers"] == {"Authorization": {"env": "TRACKER_TOKEN", "prefix": "Bearer "}}
    assert "s-1" in _Handler.sessions


def test_http_client_reads_sse_and_keeps_session(loopback):
    client = MCPHttpClient(url=f"{loopback}/mcp")
    client.initialize()
    assert [t["name"] for t in client.list_tools()] == ["list_issues", "delete_issue"]
    assert _Handler.sessions[0] is None and _Handler.sessions[-1] == "s-1"


# ---------------------------------------------------------------- connectors


def test_add_connectors(monkeypatch, capsys, layer):
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "connectors")
    assert code == 0 and "github" in out and "slack" in out and "linear" in out
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "connector", "slack", "--workspace", "acme",
                       "--layer", str(layer))
    assert code == 0 and "3 tools: 2 read, 0 write, 1 always_approve" in out
    slack = entry_yaml(layer, "slack")
    assert {s["headers"]["Authorization"]["secret_ref"] for s in slack["sources"]} == {"slack:acme"}
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "connector", "github", "--layer", str(layer))
    assert code == 0 and "added tools/sources/github.yaml (mcp)" in out
    github = entry_yaml(layer, "github")["sources"][0]
    assert github["command"][:2] == ["docker", "run"]
    assert github["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": {"env": "GITHUB_PERSONAL_ACCESS_TOKEN"}}
    code, _, _ = run(monkeypatch, capsys, "setups", "tools", "add", "connector", "github", "--name", "gh",
                     "--remote", "--token-env", "GH_TOKEN", "--layer", str(layer))
    assert code == 0
    assert entry_yaml(layer, "gh")["sources"][0]["url"] == "https://api.githubcopilot.com/mcp/"
    code, _, _ = run(monkeypatch, capsys, "setups", "tools", "add", "connector", "linear", "--layer", str(layer))
    assert code == 0 and entry_yaml(layer, "linear")["allow"] == ["mcp_linear_*"]
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "add", "connector", "jira", "--layer", str(layer))
    assert code == 1 and "unknown connector" in err
    text = "".join(p.read_text() for p in (layer / "tools" / "sources").glob("*.yaml"))
    assert "secret_ref: slack:acme" in text and "xox" not in text


# ---------------------------------------------------------------- scopes, list, toggle, remove


@pytest.fixture
def scoped(tmp_path):
    root = tmp_path / "data"
    for rel in ("baseline", "companies/acme/projects/q3"):
        (root / rel).mkdir(parents=True)
    config = tmp_path / "setups.yaml"
    config.write_text("root: data\npolicy: strict\n")
    return root, config


def test_scope_mode_uses_the_config_policy(monkeypatch, capsys, scoped):
    root, config = scoped
    base = ["--scope", "org:acme/q3", "--config", str(config)]
    code, out, err = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE), "--name", "pets", *base)
    assert code == 0, err
    assert (root / "companies/acme/projects/q3/tools/sources/pets.yaml").is_file()
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "connector", "github", *base)
    assert code == 1 and "source kind 'mcp' is not allowed by the policy" in out
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE), "--name", "pets2",
                       "--allow-delete", *base)
    assert code == 1 and "does not allow DELETE" in out
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "list", "--scope", "org:nope", "--config", str(config))
    assert code == 1 and "no directory yet" in err


def test_list_disable_enable_remove(monkeypatch, capsys, scoped):
    root, config = scoped
    base = ["--scope", "org:acme/project:q3", "--config", str(config)]
    run(monkeypatch, capsys, "setups", "tools", "add", "openapi", str(PETSTORE), "--name", "pets", *base)
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "list", *base)
    assert code == 0 and "pets  openapi  on     7 (4r 3w 0a)  tools/sources/pets.yaml" in out
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "disable", "pets", *base)
    assert code == 0 and "disabled pets" in out
    text = (root / "companies/acme/projects/q3/tools/sources/pets.yaml").read_text()
    assert text.startswith("# Added with") and "enabled: false" in text
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "list", "--json", *base)
    entry = json.loads(out)["entries"][0]
    assert entry["enabled"] is False and entry["tools"] is None
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "enable", "pets", *base)
    assert "enabled pets" in out
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "remove", "pets", *base)
    assert code == 0 and "tools/specs/pets.yaml" in out
    assert not list((root / "companies/acme/projects/q3/tools").rglob("*.yaml"))
    code, _, err = run(monkeypatch, capsys, "setups", "tools", "remove", "pets", *base)
    assert code == 1 and "no tool source" in err


def test_sync_and_probe_by_scope(monkeypatch, capsys, scoped, loopback):
    root, config = scoped
    base = ["--scope", "org:acme", "--config", str(config)]
    run(monkeypatch, capsys, "setups", "tools", "add", "openapi", f"{loopback}/openapi.yaml", "--name", "pets",
        "--snapshot", *base)
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "sync", *base)
    assert code == 0 and "added    pet_list" in out and "7 total" in out
    assert (root / "companies/acme/tools/.fabri-state/catalog.json").is_file()
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "sync", *base)
    assert "0 added, 0 removed, 0 changed, 7 total" in out
    code, out, _ = run(monkeypatch, capsys, "setups", "tools", "probe", "--ops", "pet_list", *base)
    assert code == 0 and "pet_list" in json.loads(out)["probed"]


def test_plan_functions_hold_no_secrets():
    with pytest.raises(EntryError):
        mcp_entry("x", url="https://user:pw@a.test/mcp")
    with pytest.raises(EntryError):
        openapi_entry(spec_text="openapi: 3.0.0\npaths: {}\n", name="Bad Name")
    entry = mcp_entry("x", command=["srv"], env=["CHILD=PARENT_VAR"], connect=False)
    assert entry.data["sources"][0]["env"] == {"CHILD": {"env": "PARENT_VAR"}}


# ---------------------------------------------------------------- API


@pytest.fixture
def api(tmp_path, scoped):
    root, _ = scoped
    service = FabriService(home_root=tmp_path / "home")
    server = serve_http(service, port=0, setups_api=SetupsApi(root, policy="strict"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}")
    yield client, root
    server.shutdown()
    server.server_close()
    service.close()


def test_api_tool_sources(api):
    client, root = api
    spec = PETSTORE.read_text()
    body = {"scope": "org:acme", "source": "openapi", "spec_text": spec, "name": "pets",
            "auth": {"env": "PETS_TOKEN"}, "dry_run": True}
    preview = client.post("/tools/sources", json=body).json()
    assert preview["written"] is False and preview["tool_count"] == 7 and "kind: openapi" in preview["yaml"]
    assert not (root / "companies/acme/tools").exists()
    added = client.post("/tools/sources", json={**body, "dry_run": False})
    assert added.status_code == 200 and added.json()["approvals"] == {"read": 4, "write": 3, "always_approve": 0}
    assert client.post("/tools/sources", json={**body, "dry_run": False}).status_code == 409
    listed = client.get("/tools/sources", params={"scope": "org:acme"}).json()
    assert listed["entries"][0]["name"] == "pets" and listed["entries"][0]["tools"] == 7
    assert client.post("/tools/sources/enable", json={"scope": "org:acme", "name": "pets", "enabled": False}).json() \
        == {"ok": True, "changed": True}
    assert client.get("/tools/sources", params={"scope": "org:acme"}).json()["entries"][0]["enabled"] is False
    removed = client.delete("/tools/sources", params={"scope": "org:acme", "name": "pets"}).json()
    assert removed["removed"] == ["tools/sources/pets.yaml", "tools/specs/pets.yaml"]
    assert client.delete("/tools/sources", params={"scope": "org:acme", "name": "pets"}).status_code == 404
    audit = [json.loads(line)["action"] for line in (root / "companies/acme/.fabri-audit.jsonl").read_text().splitlines()]
    assert audit == ["add-tool-source", "disable-tool-source", "remove-tool-source"]


def test_api_tool_sources_refusals(api):
    client, _ = api
    bad_auth = client.post("/tools/sources", json={"scope": "org:acme", "source": "openapi",
                                                   "spec_text": PETSTORE.read_text(), "auth": {"env": "sk-abc"}})
    assert bad_auth.status_code == 400 and "environment variable name" in bad_auth.json()["error"]
    local_path = client.post("/tools/sources", json={"scope": "org:acme", "source": "openapi", "spec_url": "/etc/x"})
    assert local_path.status_code == 400
    mcp = client.post("/tools/sources", json={"scope": "org:acme", "source": "connector", "connector": "linear"})
    assert mcp.status_code == 400 and any("not allowed by the policy" in r for r in mcp.json()["reasons"])
    assert client.post("/tools/sources", json={"scope": "org:acme", "source": "ftp"}).status_code == 400
    connectors = client.get("/tools/connectors").json()
    assert [c["name"] for c in connectors] == ["github", "linear", "slack"]
    assert connectors[2]["env_var"] == "SLACK_BOT_TOKEN" and connectors[2]["store_provider"] == "slack"
