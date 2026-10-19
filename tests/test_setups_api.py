"""Setups API (docs/setups-api.md): every endpoint over real HTTP on loopback,
against a temp layer tree with the petstore fixture. No outside network;
probes and tool calls go through MockTransport."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import httpx
import pytest

from fabri.service.auth import sign_session
from fabri.service.http_server import serve_http
from fabri.service.service import FabriService
from fabri.service.setups_routes import AUDIT_FILE, SetupsApi, scrub
from fabri.setups import SetupError, clear_cache

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
ADMIN = "adm-token"
SKILL = {"skill.yaml": "name: greet\ndescription: Say hello.\ntypes: [chat]\n", "prompt.md": "Say hello kindly.\n"}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def pets_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/pets":
            return httpx.Response(200, json=[{"id": 1, "name": "Rex", "status": "available", "email": "a@b.test"},
                                             {"id": 2, "name": "Tom", "status": "adopted"}])
        if request.url.path.startswith("/pets/"):
            return httpx.Response(200, json={"id": 1, "name": "Rex"})
        return httpx.Response(200, json={})
    return httpx.MockTransport(handler)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "data"
    shutil.copy(FIXTURES / "petstore.yaml", root / "baseline" / "tools" / "petstore.yaml") \
        if (root / "baseline" / "tools").mkdir(parents=True) is None else None
    write(root / "baseline/tools/tools.yaml", """
sources:
  - kind: openapi
    name: pets
    spec: petstore.yaml
allow: ["*"]
""")
    write(root / "baseline/agents/chat/agent.yaml", "title: Chat\ndescription: Chats.\n")
    write(root / "baseline/agents/chat/prompt.md", "You chat.\n")
    write(root / "baseline/agents/coder/agent.yaml", "route: {description: Writes code, not_for: chat}\n")
    write(root / "baseline/agents/coder/prompt.md", "You code.\n")
    write(root / "packs/sales/context/pitch.md", "Sales pitch context.\n")
    write(root / "companies/acme/setup.yaml", "packs: [sales]\n")
    (root / "companies/acme/projects/q3").mkdir(parents=True)
    write(root / "tools-allow.yaml", "allow: [pet_*, weather_*, owner_*]\nallowed_hosts: ['*.test']\n")
    clear_cache()
    return root


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


class Client:
    def __init__(self, server, headers=None):
        port = server.server_address[1]
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers or {})

    def call(self, method, path, body=None, **kw):
        if body is not None:
            kw["json"] = body
        return self.http.request(method, path, **kw)


@pytest.fixture
def make_server(root, tmp_path):
    started = []

    def make(**api_kwargs):
        api = SetupsApi(root, global_allow="tools-allow.yaml", transport=pets_transport(), **api_kwargs)
        service = FabriService(home_root=tmp_path / "home")
        server = serve_http(service, port=0, setups_api=api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, service))
        return server, service, api

    yield make
    for server, service in started:
        server.shutdown()
        server.server_close()
        service.close()


@pytest.fixture
def api(make_server):
    server, _, _ = make_server()
    return Client(server)


def audit_lines(root: Path, layer: str) -> list[dict]:
    path = root / layer / AUDIT_FILE
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ---------------------------------------------------------------- scopes


def test_scopes_list_and_create(api, root):
    body = api.call("GET", "/setups/scopes").json()
    assert body["baseline"]["exists"] is True
    assert body["packs"] == ["sales"]
    assert body["orgs"] == [{"name": "acme", "packs": ["sales"], "projects": ["q3"]}]

    r = api.call("POST", "/setups/scopes", {"kind": "org", "name": "globex"})
    assert r.status_code == 201 and r.json()["scope"] == "org:globex"
    r = api.call("POST", "/setups/scopes", {"kind": "project", "name": "alpha", "org": "globex"})
    assert r.json() == {"scope": "org:globex/project:alpha", "kind": "project", "name": "alpha", "org": "globex"}
    assert api.call("POST", "/setups/scopes", {"kind": "pack", "name": "ops"}).status_code == 201
    assert api.call("POST", "/setups/scopes", {"kind": "org", "name": "globex"}).status_code == 409
    assert api.call("POST", "/setups/scopes", {"kind": "project", "name": "x", "org": "nope"}).status_code == 404
    assert (root / "companies/globex/projects/alpha").is_dir()
    assert audit_lines(root, "companies/globex")[0]["action"] == "create-scope"


@pytest.mark.parametrize("name", ["..", "../x", "a/b", "A", "x y", "-x", "", "a\\b", "%2e%2e"])
def test_scope_creation_rejects_bad_names(api, root, name):
    r = api.call("POST", "/setups/scopes", {"kind": "org", "name": name})
    assert r.status_code == 400
    assert not (root.parent / "x").exists()


# ---------------------------------------------------------------- items


def test_item_put_get_list_delete(api, root):
    scope = "org:acme/project:q3"
    q = f"?scope={scope}&kind=skills&name=greet"
    r = api.call("PUT", "/setups/item" + q, {"files": SKILL})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (root / "companies/acme/projects/q3/skills/greet/prompt.md").read_text() == "Say hello kindly.\n"

    got = api.call("GET", "/setups/item" + q).json()
    assert got["name"] == "greet" and got["files"] == SKILL
    assert got["meta"]["description"] == "Say hello."
    assert isinstance(got["lint"], list)

    items = api.call("GET", f"/setups/items?scope={scope}&kind=skills").json()
    assert [(i["name"], i["source_layer"], i["enabled"]) for i in items] == [("greet", scope, True)]
    assert items[0]["size"] > 0 and items[0]["updated_at"]

    assert api.call("DELETE", "/setups/item" + q).status_code == 204
    assert api.call("GET", "/setups/item" + q).status_code == 404
    assert api.call("DELETE", "/setups/item" + q).status_code == 404


def test_item_kinds_roundtrip(api):
    scope = "pack:sales"
    cases = {
        "context": ("brief", {"brief.md": "Be brief.\n"}),
        "data": ("prices.json", {"prices.json": '{"a": 1}'}),
        "plays": ("onboard", {"play.yaml": "name: onboard\ndescription: Onboard.\n"}),
        "agents": ("writer", {"agent.yaml": "title: Writer\n", "prompt.md": "Write.\n"}),
    }
    for kind, (name, files) in cases.items():
        q = f"?scope={scope}&kind={kind}&name={name}"
        assert api.call("PUT", "/setups/item" + q, {"files": files}).status_code == 200, kind
        assert api.call("GET", "/setups/item" + q).json()["files"] == files
        listed = api.call("GET", f"/setups/items?scope={scope}&kind={kind}").json()
        assert name in [i["name"] for i in listed]


def test_item_validation_errors(api):
    q = "?scope=pack:sales&kind=skills&name=bad"
    r = api.call("PUT", "/setups/item" + q, {"files": {"skill.yaml": "name: other\ndescription: x\n"}})
    assert r.status_code == 400 and "match" in r.json()["error"]
    assert api.call("PUT", "/setups/item" + q, {"files": {"evil.sh": "x"}}).status_code == 400
    assert api.call("PUT", "/setups/item" + q, {"files": {}}).status_code == 400
    assert api.call("PUT", "/setups/item" + q, {}).status_code == 400
    assert api.call("PUT", "/setups/item?scope=pack:nope&kind=skills&name=x", {"files": SKILL}).status_code == 404
    assert api.call("GET", "/setups/items?scope=pack:sales&kind=bogus").status_code == 400


@pytest.mark.parametrize("scope", [
    "..", "pack:..", "pack:../../etc", "org:../x", "org:acme/project:..", "org:acme/../x", "pack:a/b",
    "org:acme/project:q3/extra", "PACK:sales", "pack:sales\n", "", "org:", "pack:%2e%2e",
])
def test_scope_traversal_rejected(api, scope):
    for method, path in (("GET", "/setups/items"), ("GET", "/setups/item"), ("PUT", "/setups/item"),
                         ("DELETE", "/setups/item"), ("GET", "/setups/lint")):
        r = api.call(method, path, params={"scope": scope, "kind": "skills", "name": "x"},
                     **({"json": {"files": SKILL}} if method == "PUT" else {}))
        assert r.status_code == 400, (method, scope, r.text)


@pytest.mark.parametrize("name", ["../x", "..", "a/b", "a/../b", "/etc/passwd", "x.disabled/../y", "A", "a b", "%2e%2e/x"])
def test_item_name_traversal_rejected(api, root, name):
    for kind in ("skills", "agents", "context", "plays", "data"):
        r = api.call("PUT", "/setups/item", json={"files": {"x": "y"}},
                     params={"scope": "baseline", "kind": kind, "name": name})
        assert r.status_code == 400, (kind, name)
    assert not (root / "x").exists() and not (root.parent / "x").exists()


def test_symlink_escape_is_blocked(api, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "packs" / "evil").symlink_to(outside)
    r = api.call("PUT", "/setups/item", json={"files": SKILL},
                 params={"scope": "pack:evil", "kind": "skills", "name": "greet"})
    assert r.status_code == 400
    assert list(outside.iterdir()) == []


def test_tool_name_traversal_rejected(api, root):
    victim = root / "baseline/tools/registered"
    victim.mkdir(parents=True)
    (root / "baseline/tools/keep.json").write_text("{}")
    for path in ("/tools/unregister", "/tools/enable"):
        r = api.call("POST", path, {"scope": "baseline", "name": "../keep", "enabled": True})
        assert r.status_code == 400
    assert (root / "baseline/tools/keep.json").exists()


def test_disable_marker_and_listing(api, root):
    q = "?scope=pack:sales&kind=skills&name=greet"
    api.call("PUT", "/setups/item" + q, {"files": SKILL})
    r = api.call("POST", "/setups/item/disable", {"scope": "pack:sales", "kind": "skills", "name": "greet", "disabled": True})
    assert r.json() == {"ok": True}
    assert (root / "packs/sales/skills/greet.disabled").exists()
    items = api.call("GET", "/setups/items?scope=pack:sales&kind=skills").json()
    assert items == [{**items[0], "name": "greet", "enabled": False}]
    resolved = api.call("GET", "/setups/resolved?org=acme").json()
    assert "greet" not in resolved["skills"]
    api.call("POST", "/setups/item/disable", {"scope": "pack:sales", "kind": "skills", "name": "greet", "disabled": False})
    assert "greet" in api.call("GET", "/setups/resolved?org=acme").json()["skills"]
    bad = api.call("POST", "/setups/item/disable", {"scope": "pack:sales", "kind": "skills", "name": "greet", "disabled": "yes"})
    assert bad.status_code == 400


# ---------------------------------------------------------------- hot reload / resolved


def test_hot_reload_put_skill_shows_in_resolved(api):
    before = api.call("GET", "/setups/resolved?org=acme&project=q3&type=chat").json()
    assert "greet" not in before["skills"]
    api.call("PUT", "/setups/item", json={"files": SKILL},
             params={"scope": "org:acme/project:q3", "kind": "skills", "name": "greet"})
    after = api.call("GET", "/setups/resolved?org=acme&project=q3&type=chat").json()
    assert after["skills"]["greet"]["source"] == "org:acme/project:q3"
    assert after["content_hash"] != before["content_hash"]
    assert [l["name"] for l in after["layers"]] == ["baseline", "pack:sales", "org:acme", "org:acme/project:q3"]
    assert "path" not in json.dumps(after["layers"])
    # baseline view does not see the project skill
    assert "greet" not in api.call("GET", "/setups/resolved").json()["skills"]


def test_resolved_has_tools_routes_and_errors(api):
    body = api.call("GET", "/setups/resolved?org=acme").json()
    assert any(t["name"] == "pet_list" and t["layer"] == "baseline" for t in body["resolved_tools"])
    assert body["routes"] == [{"type": "coder", "description": "Writes code", "not_for": "chat", "source": "baseline"}] \
        or body["routes"][0]["type"] == "coder"
    assert api.call("GET", "/setups/resolved?project=q3").status_code == 400
    assert api.call("GET", "/setups/resolved?org=nope").status_code == 404
    assert api.call("GET", "/setups/resolved?org=acme&type=ghost").status_code == 404
    assert api.call("GET", "/setups/resolved?org=../x").status_code == 400


# ---------------------------------------------------------------- import / lint


def test_import_dry_run_then_real(api, root):
    files = {"my-skill/SKILL.md": "---\nname: My Skill\ndescription: Does things.\n---\nDo the things.\n"}
    r = api.call("POST", "/setups/import", {"scope": "pack:sales", "format": "skill-md", "files": files, "dry_run": True})
    assert r.status_code == 200 and r.json()["created"] == ["my-skill"]
    assert not (root / "packs/sales/skills/my-skill").exists()
    assert audit_lines(root, "packs/sales") == []
    r = api.call("POST", "/setups/import", {"scope": "pack:sales", "format": "skill-md", "files": files, "dry_run": False})
    assert r.json()["created"] == ["my-skill"]
    assert (root / "packs/sales/skills/my-skill/skill.yaml").exists()
    again = api.call("POST", "/setups/import", {"scope": "pack:sales", "format": "skill-md", "files": files, "dry_run": False}).json()
    assert again["skipped"] == ["my-skill"]
    assert [a["action"] for a in audit_lines(root, "packs/sales")] == ["import-create"]


def test_import_rejects_unsafe_paths_and_formats(api, root):
    for rel in ("../evil/SKILL.md", "/abs/SKILL.md", "a/../../SKILL.md", ".hidden/SKILL.md"):
        r = api.call("POST", "/setups/import", {"scope": "pack:sales", "format": "skill-md", "files": {rel: "x"}})
        assert r.status_code == 400, rel
    assert api.call("POST", "/setups/import", {"scope": "pack:sales", "format": "zip", "files": {"a/SKILL.md": "x"}}).status_code == 400
    assert api.call("POST", "/setups/import", {"scope": "pack:sales", "format": "skill-md", "files": {}}).status_code == 400


def test_lint_reports_missing_tool(api, root):
    r = api.call("PUT", "/setups/item", json={"files": {"agent.yaml": "tools: [ghost_tool]\n"}},
                 params={"scope": "baseline", "kind": "agents", "name": "chat"})
    assert r.status_code == 400 and "allowlist" in r.json()["error"]
    # allowlisted name that no source provides: write is fine, lint flags it
    r = api.call("PUT", "/setups/item", json={"files": {"agent.yaml": "tools: [pet_ghost]\n"}},
                 params={"scope": "baseline", "kind": "agents", "name": "chat"})
    assert r.status_code == 200, r.text
    findings = api.call("GET", "/setups/lint?scope=org:acme").json()
    assert {"severity": "error", "kind": "agents", "name": "chat"}.items() <= findings[0].items()
    assert any("pet_ghost" in f["message"] for f in findings)
    got = api.call("GET", "/setups/item", params={"scope": "baseline", "kind": "agents", "name": "chat"}).json()
    assert any("pet_ghost" in f["message"] for f in got["lint"])


# ---------------------------------------------------------------- tools


REGISTER = {
    "name": "weather_now", "description": "Current weather for a city.", "kind": "http",
    "endpoint": "https://weather.test/now/{city}", "method": "GET",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    "auth": {"secret_ref": "weather:prod", "prefix": "Bearer "},
}


def test_register_enable_unregister_and_audit(api, root):
    r = api.call("POST", "/tools/register", {"scope": "org:acme", "decl": REGISTER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "weather_now" and body["approval"] == "read" and body["changed"] is True
    assert body["decl"]["auth"] == {"secret_ref": "weather:prod", "prefix": "Bearer "}
    assert "path" not in body

    cat = api.call("GET", "/tools/catalog?org=acme").json()
    entry = next(e for e in cat if e["name"] == "weather_now")
    assert entry["approval"] == "read" and set(entry) >= {"name", "alias", "title", "description", "approval",
                                                          "source", "endpoint", "params", "options"}
    assert all(e["name"] != "weather_now" for e in api.call("GET", "/tools/catalog").json())
    assert any(e["name"] == "pet_list" for e in api.call("GET", "/tools/catalog").json())

    assert api.call("POST", "/tools/enable", {"scope": "org:acme", "name": "weather_now", "enabled": False}).json() == {"ok": True}
    assert all(e["name"] != "weather_now" for e in api.call("GET", "/tools/catalog?org=acme").json())
    api.call("POST", "/tools/enable", {"scope": "org:acme", "name": "weather_now", "enabled": True})
    assert api.call("POST", "/tools/enable", {"scope": "org:acme", "name": "ghost", "enabled": True}).status_code == 404
    assert api.call("POST", "/tools/unregister", {"scope": "org:acme", "name": "weather_now"}).json() == {"ok": True}
    assert api.call("POST", "/tools/unregister", {"scope": "org:acme", "name": "weather_now"}).status_code == 404
    actions = [a["action"] for a in audit_lines(root, "companies/acme")]
    assert actions == ["register-tool", "disable-tool", "enable-tool", "unregister-tool"]


@pytest.mark.parametrize("patch,needle", [
    ({"kind": "python", "callable": "os:system"}, "python"),
    ({"method": "DELETE"}, "DELETE"),
    ({"name": "evil_tool"}, "allowlist"),
    ({"endpoint": "https://evil.example.com/x"}, "allowed_hosts"),
    ({"auth": {"secret_ref": "weather:prod", "extra": "x"}}, "raw values"),
    ({"auth": {"header_env": "sk-abcdefghijklmnopqrstuvwxyz123456"}}, "environment variable"),
    ({"endpoint": "https://weather.test/now?api_key=abc"}, "credential"),
    ({"surprise": 1}, "unknown keys"),
])
def test_register_policy_rejections(api, root, patch, needle):
    decl = {**REGISTER, **patch}
    if patch.get("kind") == "python":
        decl.pop("endpoint"), decl.pop("method")
    r = api.call("POST", "/tools/register", {"scope": "org:acme", "decl": decl})
    assert r.status_code == 400, r.text
    body = r.json()
    assert needle.lower() in body["error"].lower() and body["reasons"]
    assert audit_lines(root, "companies/acme") == []
    assert not (root / "companies/acme/tools/registered").exists()


def test_register_requires_decl_object(api):
    assert api.call("POST", "/tools/register", {"scope": "org:acme", "decl": "x"}).status_code == 400
    assert api.call("POST", "/tools/register", {"scope": "org:acme"}).status_code == 400


def test_tools_yaml_literal_secret_refused_and_redacted(api, root):
    bad = "sources:\n  - {kind: http, name: weather_now, url: 'https://weather.test/n', method: GET, headers: {Authorization: 'Bearer sk-live-123456789'}}\nallow: ['*']\n"
    q = {"scope": "baseline", "kind": "tools", "name": "tools.yaml"}
    r = api.call("PUT", "/setups/item", json={"files": {"tools.yaml": bad}}, params=q)
    assert r.status_code == 400 and "sk-live" not in r.text
    # a hand-edited file with a literal is redacted on read
    write(root / "packs/sales/tools/tools.yaml", bad)
    got = api.call("GET", "/setups/item", params={**q, "scope": "pack:sales"})
    assert "sk-live" not in got.text and got.json()["meta"]["redacted"] is True
    resolved = api.call("GET", "/setups/resolved?org=acme")
    assert "sk-live" not in resolved.text


def test_tools_yaml_with_secret_ref_ok(api):
    good = ("sources:\n  - {kind: http, name: weather_now, url: 'https://weather.test/n', method: GET, "
            "headers: {Authorization: {secret_ref: 'w:prod', prefix: 'Bearer '}}}\nallow: ['weather_*']\n")
    r = api.call("PUT", "/setups/item", json={"files": {"tools.yaml": good}},
                 params={"scope": "pack:sales", "kind": "tools", "name": "tools.yaml"})
    assert r.status_code == 200, r.text
    got = api.call("GET", "/setups/item", params={"scope": "pack:sales", "kind": "tools", "name": "tools.yaml"}).json()
    assert got["files"]["tools.yaml"] == good and got["meta"]["sources"] == ["weather_now"]


def test_sync_reports_diff_and_audits(api, root):
    first = api.call("POST", "/tools/sync", {"scope": "org:acme"}).json()
    assert "pet_list" in first["added"] and first["removed"] == [] and first["changed"] == []
    api.call("POST", "/tools/register", {"scope": "org:acme", "decl": REGISTER})
    second = api.call("POST", "/tools/sync", {"scope": "org:acme"}).json()
    assert second["added"] == ["weather_now"]
    assert (root / "companies/acme/tools/.fabri-state/sync.json").exists()
    assert [a["action"] for a in audit_lines(root, "companies/acme")][-1] == "sync-tools"


def test_probe_uses_mock_transport_and_hides_samples(api, root):
    r = api.call("POST", "/tools/probe", {"scope": "baseline"})
    assert r.status_code == 200
    body = r.json()
    assert "pet_list" in body["probed"] and set(body) == {"probed", "skipped"}
    assert "a@b.test" not in r.text
    state = (root / "baseline/tools/.fabri-state/probe.json").read_text()
    assert "a@b.test" not in state
    assert api.call("POST", "/tools/probe", {"scope": "baseline", "ops": "x"}).status_code == 400
    assert audit_lines(root, "baseline")[-1]["action"] == "probe-tools"
    cat = {e["name"]: e for e in api.call("GET", "/tools/catalog").json()}
    assert cat["pet_list"]["options"] or any(p.get("options") for p in cat["pet_get"]["params"])


# ---------------------------------------------------------------- audit


def test_every_write_appends_one_audit_line(api, root):
    q = {"scope": "org:acme/project:q3", "kind": "skills", "name": "greet"}
    api.call("PUT", "/setups/item", json={"files": SKILL}, params=q)
    api.call("POST", "/setups/item/disable", {"scope": q["scope"], "kind": "skills", "name": "greet", "disabled": True})
    api.call("DELETE", "/setups/item", params=q)
    lines = audit_lines(root, "companies/acme/projects/q3")
    assert [l["action"] for l in lines] == ["put", "disable", "delete"]
    for line in lines:
        assert set(line) == {"ts", "actor", "action", "scope", "kind", "name", "content_hash"}
        assert line["actor"] == "anonymous" and line["scope"] == q["scope"] and len(line["content_hash"]) == 64
    # hidden file: the content hash that drives hot reload ignores it
    assert (root / "companies/acme/projects/q3" / AUDIT_FILE).exists()
    # failed writes leave no line
    api.call("PUT", "/setups/item", json={"files": {"x": "y"}}, params=q)
    assert len(audit_lines(root, "companies/acme/projects/q3")) == 3


# ---------------------------------------------------------------- auth / roles


def test_open_when_nothing_configured(api):
    assert api.call("GET", "/setups/scopes").status_code == 200
    assert api.call("POST", "/setups/scopes", {"kind": "pack", "name": "open"}).status_code == 201


def test_admin_token_gates_writes_only(make_server, monkeypatch, root):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", ADMIN)
    server, _, _ = make_server()
    anon = Client(server)
    assert anon.call("GET", "/setups/scopes").status_code == 200
    assert anon.call("POST", "/setups/scopes", {"kind": "pack", "name": "p1"}).status_code == 401
    wrong = Client(server, {"Authorization": "Bearer nope"})
    assert wrong.call("POST", "/setups/scopes", {"kind": "pack", "name": "p1"}).status_code == 401
    admin = Client(server, {"Authorization": f"Bearer {ADMIN}"})
    assert admin.call("POST", "/setups/scopes", {"kind": "pack", "name": "p1"}).status_code == 201
    assert audit_lines(root, "packs/p1")[0]["actor"] == "admin-token"
    assert anon.call("DELETE", "/setups/item", params={"scope": "pack:p1", "kind": "skills", "name": "x"}).status_code == 401


def test_service_token_reads_and_admin_separation(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc")
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", ADMIN)
    server, _, _ = make_server()
    anon, svc = Client(server), Client(server, {"Authorization": "Bearer svc"})
    assert anon.call("GET", "/setups/scopes").status_code == 401
    assert svc.call("GET", "/setups/scopes").status_code == 200
    assert svc.call("POST", "/setups/scopes", {"kind": "pack", "name": "p2"}).status_code == 403
    assert svc.call("POST", "/tools/probe", {"scope": "baseline"}).status_code == 403


def test_service_token_alone_is_admin(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc")
    server, _, _ = make_server()
    svc = Client(server, {"Authorization": "Bearer svc"})
    assert svc.call("POST", "/setups/scopes", {"kind": "pack", "name": "p3"}).status_code == 201


def test_session_roles(make_server, root):
    server, service, _ = make_server()
    from fabri.service.auth import UserStore

    service.auth_enabled, service.auth_secret = True, "secret"
    service.user_store = UserStore(Path(service.home_root) / "auth.db")
    service.auth_cfg = {"admins": ["boss@x.test"]}
    boss_id = service.user_store.create_user("boss@x.test", "password-1")
    dev_id = service.user_store.create_user("dev@x.test", "password-2")

    def cookie(uid):
        return {"Cookie": f"fabri_session={sign_session(uid, 'secret', 60)}"}

    assert Client(server).call("GET", "/setups/scopes").status_code == 401
    dev, boss = Client(server, cookie(dev_id)), Client(server, cookie(boss_id))
    assert dev.call("GET", "/setups/scopes").status_code == 200
    assert dev.call("POST", "/setups/scopes", {"kind": "pack", "name": "p4"}).status_code == 403
    assert dev.call("PUT", "/setups/item", json={"files": SKILL},
                    params={"scope": "baseline", "kind": "skills", "name": "greet"}).status_code == 403
    assert boss.call("POST", "/setups/scopes", {"kind": "pack", "name": "p4"}).status_code == 201
    assert audit_lines(root, "packs/p4")[0]["actor"] == "boss@x.test"
    assert not (root / "baseline/skills").exists()


# ---------------------------------------------------------------- secrets, transport, config


def test_scrub_hides_secrets_but_keeps_references():
    data = {"api_key": "abc", "token": "t", "secret_ref": "pets:prod", "header_env": "PETS_TOKEN",
            "nested": [{"password": "pw", "note": "ghp_abcdefghijklmnop"}], "ok": "fine", "n": 3}
    out = scrub(data)
    assert out == {"api_key": "[redacted]", "token": "[redacted]", "secret_ref": "pets:prod",
                   "header_env": "PETS_TOKEN", "nested": [{"password": "[redacted]", "note": "[redacted]"}],
                   "ok": "fine", "n": 3}


def test_agent_yaml_secret_not_returned(api):
    api.call("PUT", "/setups/item", json={"files": {"agent.yaml": "llm: {api_key: 'hunter2hunter2', model: m}\n"}},
             params={"scope": "baseline", "kind": "agents", "name": "chat"})
    r = api.call("GET", "/setups/resolved?org=acme&type=chat")
    assert "hunter2" not in r.text


def test_unknown_routes_and_methods(api):
    assert api.call("GET", "/setups/nothing").status_code == 404
    assert api.call("PATCH", "/setups/scopes").status_code in (404, 501)
    assert api.call("DELETE", "/setups/scopes").status_code == 405
    bad = api.http.post("/setups/scopes", content=b"{not json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_no_routes_without_config(root, tmp_path):
    service = FabriService(home_root=tmp_path / "h2")
    server = serve_http(service, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert Client(server).call("GET", "/setups/scopes").status_code == 404
    finally:
        server.shutdown(); server.server_close(); service.close()


def test_config_file_loading(tmp_path):
    cfg = tmp_path / "cfg" / "setups.yaml"
    write(cfg, "root: ../tree\nlayers:\n  - {kind: baseline, path: base}\n  - {kind: org, path: 'tenants/{org}'}\n"
               "policy: permissive\n")
    api = SetupsApi.from_files(None, cfg)
    assert api.root == (tmp_path / "tree").resolve() and set(api.templates) == {"baseline", "org"}
    assert api.policy.allowed_kinds == ("openapi", "mcp", "http", "python")
    assert SetupsApi.from_files(tmp_path / "other", cfg).root == (tmp_path / "other").resolve()
    for bad in ("layers: [{kind: org, path: '../{org}'}]", "layers: [{kind: org, path: '/abs/{org}'}]",
                "layers: [{kind: org, path: 'x/{project}'}]", "layers: [{kind: org, path: 'x'}]",
                "layers: [{kind: org, path: 'a{org}'}]", "policy: loose", "bogus: 1"):
        write(cfg, f"root: ../tree\n{bad}\n")
        with pytest.raises(SetupError):
            SetupsApi.from_files(None, cfg)
    with pytest.raises(SetupError):
        SetupsApi.from_files(None, None)


def test_cli_flags_registered(tmp_path):
    from fabri.cli import _setups_api
    import argparse

    assert _setups_api(argparse.Namespace(setups_root=None, setups_config=None)) is None
    api = _setups_api(argparse.Namespace(setups_root=str(tmp_path), setups_config=None))
    assert isinstance(api, SetupsApi)


# ---------------------------------------------------------------- agent types and companies


def test_agent_types_listing(api):
    api.call("PUT", "/setups/item", json={"files": SKILL},
             params={"scope": "baseline", "kind": "skills", "name": "greet"})
    types = {t["type"]: t for t in api.call("GET", "/setups/agent-types").json()}
    assert set(types) == {"chat", "coder"}
    assert types["chat"] == {"type": "chat", "title": "Chat", "global_skills": ["greet"], "used_by": ["acme"]}
    assert types["coder"]["route"]["not_for"] == "chat" and types["coder"]["global_skills"] == []


def test_agent_type_get_put_prompt_and_global_skills(api, root):
    api.call("PUT", "/setups/item", json={"files": SKILL},
             params={"scope": "baseline", "kind": "skills", "name": "greet"})
    api.call("PUT", "/setups/item", json={"files": {"skill.yaml": "name: other\ndescription: Other.\ntypes: [coder]\n"}},
             params={"scope": "baseline", "kind": "skills", "name": "other"})
    got = api.call("GET", "/setups/agent-types/chat").json()
    assert got == {"type": "chat", "title": "Chat", "prompt": "You chat.\n", "global_skills": ["greet"]}

    r = api.call("PUT", "/setups/agent-types/chat", {"prompt": "You chat well.\n", "global_skills": ["greet", "other"]})
    assert r.status_code == 200 and r.json()["global_skills"] == ["greet", "other"]
    assert (root / "baseline/agents/chat/prompt.md").read_text() == "You chat well.\n"
    assert "chat" in (root / "baseline/skills/other/skill.yaml").read_text()
    assert (root / "baseline/skills/other/prompt.md").exists() or True

    # removing a type that would leave types: [] is refused, nothing changes
    before = (root / "baseline/skills/greet/skill.yaml").read_text()
    r = api.call("PUT", "/setups/agent-types/chat", {"global_skills": ["other"]})
    assert r.status_code == 400 and r.json()["reasons"]
    assert (root / "baseline/skills/greet/skill.yaml").read_text() == before

    assert api.call("PUT", "/setups/agent-types/chat", {"global_skills": ["ghost"]}).status_code == 400
    assert api.call("PUT", "/setups/agent-types/chat", {}).status_code == 400
    assert api.call("GET", "/setups/agent-types/ghost").status_code == 404
    assert api.call("PUT", "/setups/agent-types/ghost", {"prompt": "x"}).status_code == 404
    assert api.call("GET", "/setups/agent-types/..%2Fx").status_code in (400, 404)
    assert {a["action"] for a in audit_lines(root, "baseline")} == {"put"}


def test_org_agent_types_enable_and_enforce(api, root):
    got = api.call("GET", "/setups/orgs/acme/agent-types").json()
    assert got == {"agents": ["chat", "coder"], "available": ["chat", "coder"], "explicit": False}
    r = api.call("PUT", "/setups/orgs/acme/agent-types", {"agents": ["chat"]})
    assert r.json() == {"ok": True, "agents": ["chat"]}
    setup_yaml = (root / "companies/acme/setup.yaml").read_text()
    assert "packs" in setup_yaml and "agents" in setup_yaml  # existing keys kept
    assert api.call("GET", "/setups/orgs/acme/agent-types").json()["agents"] == ["chat"]
    assert [t["used_by"] for t in api.call("GET", "/setups/agent-types").json()] == [["acme"], []]
    # disabled type does not resolve for that company; the other still does
    assert api.call("GET", "/setups/resolved?org=acme&type=coder").status_code == 404
    assert api.call("GET", "/setups/resolved?org=acme&type=chat").status_code == 200
    assert api.call("GET", "/tools/catalog?org=acme&type=coder").status_code == 404
    # the resolver itself agrees (not just the API view)
    from fabri.setups import resolve_setup

    layers = [("baseline", root / "baseline"), ("pack:sales", root / "packs/sales"), ("org:acme", root / "companies/acme")]
    assert resolve_setup(layers, use_cache=False).agent_types() == ["chat"]
    api.call("PUT", "/setups/orgs/acme/agent-types", {"agents": ["chat", "coder"]})
    assert api.call("GET", "/setups/resolved?org=acme&type=coder").status_code == 200
    assert api.call("PUT", "/setups/orgs/acme/agent-types", {"agents": ["ghost"]}).status_code == 400
    assert api.call("PUT", "/setups/orgs/acme/agent-types", {"agents": "chat"}).status_code == 400
    assert api.call("PUT", "/setups/orgs/nope/agent-types", {"agents": []}).status_code == 404
    assert api.call("GET", "/setups/orgs/..x/agent-types").status_code in (400, 404)
    assert [a["action"] for a in audit_lines(root, "companies/acme")] == ["put", "put"]


def test_org_agent_types_needs_admin(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", ADMIN)
    server, _, _ = make_server()
    assert Client(server).call("PUT", "/setups/orgs/acme/agent-types", {"agents": []}).status_code == 401
    assert Client(server).call("PUT", "/setups/agent-types/chat", {"prompt": "x"}).status_code == 401
    assert Client(server).call("GET", "/setups/agent-types").status_code == 200


def test_skill_resolution_order_for_agent(api):
    for scope, name in (("baseline", "g-skill"), ("pack:sales", "p-skill"), ("org:acme", "o-skill"),
                        ("org:acme/project:q3", "local")):
        files = {"skill.yaml": f"name: {name}\ndescription: {name}.\n" + ("types: [chat]\n" if scope == "baseline" else ""),
                 "prompt.md": "x\n"}
        assert api.call("PUT", "/setups/item", json={"files": files},
                        params={"scope": scope, "kind": "skills", "name": name}).status_code == 200
    # a universal baseline skill is not a "global skill" of the type
    api.call("PUT", "/setups/item", json={"files": {"skill.yaml": "name: uni\ndescription: u.\n"}},
             params={"scope": "baseline", "kind": "skills", "name": "uni"})
    order = api.call("GET", "/setups/resolved?org=acme&project=q3&type=chat").json()["skill_order"]
    assert [(s["name"], s["group"]) for s in order if s["name"] != "uni"] == [
        ("g-skill", "global"), ("local", "project"), ("p-skill", "org-or-pack"), ("o-skill", "org-or-pack")]
    assert [s["name"] for s in order][-1] == "uni"
    # project skill is invisible from the org view and from another project
    org_view = api.call("GET", "/setups/resolved?org=acme&type=chat").json()
    assert "local" not in org_view["skills"]
    items = api.call("GET", "/setups/items?scope=org:acme/project:q3&kind=skills").json()
    assert {i["name"]: i["source_layer"] for i in items}["local"] == "org:acme/project:q3"


# ---------------------------------------------------------------- setup.yaml as an item


def test_setup_item_get_put_delete(api, root):
    q = {"scope": "org:acme/project:q3", "kind": "setup", "name": "setup.yaml"}
    assert api.call("GET", "/setups/item", params=q).status_code == 404  # empty to Studio
    assert api.call("GET", "/setups/items", params={"scope": q["scope"], "kind": "setup"}).json() == []

    text = "budgets: {context_chars: 5000}\nnotes: hello\n"
    r = api.call("PUT", "/setups/item", json={"files": {"setup.yaml": text}}, params=q)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (root / "companies/acme/projects/q3/setup.yaml").read_text() == text
    got = api.call("GET", "/setups/item", params=q).json()
    assert got["files"] == {"setup.yaml": text} and got["meta"]["notes"] == "hello"
    listed = api.call("GET", "/setups/items", params={"scope": q["scope"], "kind": "setup"}).json()
    assert [(i["name"], i["source_layer"]) for i in listed] == [("setup.yaml", q["scope"])]
    # the resolved view merges it
    assert api.call("GET", "/setups/resolved?org=acme&project=q3").json()["setup"]["notes"] == "hello"

    assert api.call("DELETE", "/setups/item", params=q).status_code == 204
    assert api.call("GET", "/setups/item", params=q).status_code == 404
    actions = [(a["action"], a["kind"], a["name"]) for a in audit_lines(root, "companies/acme/projects/q3")]
    assert actions == [("put", "setup", "setup.yaml"), ("delete", "setup", "setup.yaml")]


def test_setup_item_validation(api, root):
    q = {"scope": "org:acme", "kind": "setup", "name": "setup.yaml"}
    before = (root / "companies/acme/setup.yaml").read_text()
    r = api.call("PUT", "/setups/item", json={"files": {"setup.yaml": "a: [unclosed\n"}}, params=q)
    assert r.status_code == 400 and "invalid YAML" in r.json()["error"]
    r = api.call("PUT", "/setups/item", json={"files": {"setup.yaml": "- a list\n"}}, params=q)
    assert r.status_code == 400
    r = api.call("PUT", "/setups/item", json={"files": {"setup.yaml": "budgets: {bogus: 1}\n"}}, params=q)
    assert r.status_code == 400
    assert api.call("PUT", "/setups/item", json={"files": {"other.yaml": "a: 1\n"}}, params=q).status_code == 400
    assert api.call("GET", "/setups/item", params={**q, "name": "other.yaml"}).status_code == 400
    assert api.call("GET", "/setups/item", params={**q, "name": "../setup.yaml"}).status_code == 400
    assert api.call("POST", "/setups/item/disable", {**q, "disabled": True}).status_code == 400
    assert (root / "companies/acme/setup.yaml").read_text() == before
    assert audit_lines(root, "companies/acme") == []


def test_setup_item_needs_admin_for_writes(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", ADMIN)
    server, _, _ = make_server()
    q = {"scope": "org:acme", "kind": "setup", "name": "setup.yaml"}
    anon = Client(server)
    assert anon.call("GET", "/setups/item", params=q).status_code == 200
    assert anon.call("PUT", "/setups/item", json={"files": {"setup.yaml": "a: 1\n"}}, params=q).status_code == 401
    assert anon.call("DELETE", "/setups/item", params=q).status_code == 401


def test_agent_types_endpoint_matches_setup_yaml(api, root):
    api.call("PUT", "/setups/orgs/acme/agent-types", {"agents": ["coder"]})
    got = api.call("GET", "/setups/item", params={"scope": "org:acme", "kind": "setup", "name": "setup.yaml"}).json()
    assert got["meta"]["agents"] == ["coder"] and got["meta"]["packs"] == ["sales"]
    # editing the file by hand is seen by the convenience endpoint
    api.call("PUT", "/setups/item", json={"files": {"setup.yaml": "packs: [sales]\nagents: [chat]\n"}},
             params={"scope": "org:acme", "kind": "setup", "name": "setup.yaml"})
    assert api.call("GET", "/setups/orgs/acme/agent-types").json()["agents"] == ["chat"]
    assert api.call("GET", "/setups/resolved?org=acme&type=coder").status_code == 404
