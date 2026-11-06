"""The Cakemail connector: the pinned trimmed spec, the curated expansion and
the approval floor on the run path. Offline: the full 1.30.2 spec is a test
fixture and every HTTP call goes to an httpx.MockTransport."""
from __future__ import annotations

import copy
import importlib.util
import json
import re
from importlib import resources
from pathlib import Path

import httpx
import pytest
import yaml

from fabri.setups import clear_cache
from fabri.setups.resolver import resolve_setup
from fabri.setups.tool_entries import add_entry, connector_tool_entry
from fabri.setups.toolset import approval_warnings, resolve_tools
from fabri.tools.connector_catalog import cakemail
from fabri.tools.connectors import CONNECTORS, ConnectorError, connector_entry, request_floor, spec_text
from fabri.tools.policy import ToolPolicy

ROOT = Path(__file__).resolve().parent.parent
FULL_SPEC = Path(__file__).parent / "fixtures" / "cakemail_openapi_1.30.2.json"
SCRIPT = ROOT / "scripts" / "trim_cakemail_spec.py"
BASE = "https://api.cakemail.dev"

SEND_TEST = "cakemail_campaign_send_test"
SCHEDULE = "cakemail_campaign_schedule"
LIST_LISTS = "cakemail_list_list"
CONTACT_UPDATE = "cakemail_contact_update"


@pytest.fixture(autouse=True)
def _fresh():
    clear_cache()
    yield
    clear_cache()


def _script():
    spec = importlib.util.spec_from_file_location("trim_cakemail_spec", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _full() -> dict:
    return json.loads(FULL_SPEC.read_text(encoding="utf-8"))


def _packaged() -> dict:
    return json.loads(spec_text("cakemail"))


def _ops(doc: dict):
    for path, item in doc["paths"].items():
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op:
                yield method.upper(), path, op


def _refs(node, out: set[str]) -> set[str]:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            out.add(node["$ref"])
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)
    return out


def _layer(tmp_path: Path, entry: dict, spec: dict | None = None, name: str = "mail") -> Path:
    """A layer whose tools.yaml holds `entry` and whose specs/ holds the
    packaged spec (or `spec`)."""
    layer = tmp_path / name
    (layer / "tools" / "specs").mkdir(parents=True)
    source = entry["sources"][0]
    text = json.dumps(spec) if spec is not None else spec_text("cakemail")
    (layer / "tools" / source["spec"]).write_text(text, encoding="utf-8")
    (layer / "tools" / "tools.yaml").write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")
    return layer


def _resolve(layer: Path, transport: httpx.BaseTransport | None = None):
    return resolve_tools(resolve_setup([layer]), use_cache=False, catalog=False, transport=transport)


# ---------------------------------------------------------------- the pinned spec


def test_trim_script_is_deterministic_and_prunes():
    mod = _script()
    full = _full()
    first = mod.dump(mod.trim(full))
    second = mod.dump(mod.trim(copy.deepcopy(full)))
    assert first == second and first.endswith("\n")
    trimmed = json.loads(first)
    assert set(trimmed) == {"openapi", "info", "servers", "paths", "components", "x-fabri-trimmed"}
    assert trimmed["x-fabri-trimmed"] == {"from": "1.30.2", "script": "scripts/trim_cakemail_spec.py"}
    assert trimmed["servers"] == [{"url": BASE}]
    ops = list(_ops(trimmed))
    assert 28 <= len(ops) < len(list(_ops(full)))
    assert {method for method, _, _ in ops} <= {"GET", "POST", "PATCH"}
    for method, path, op in ops:
        assert ("x-fabri-approval" in op) == (method != "GET"), (method, path)
        if method != "GET":
            assert op["x-fabri-approval"] == "always_approve"
    # Components are exactly the $ref closure of the kept operations.
    schemas = trimmed["components"]["schemas"]
    assert len(schemas) < len(full["components"]["schemas"])
    reachable = {r.rsplit("/", 1)[-1] for r in _refs(trimmed["paths"], set())}
    frontier = set(reachable)
    while frontier:
        found = {r.rsplit("/", 1)[-1] for name in frontier for r in _refs(schemas[name], set())}
        frontier = found - reachable
        reachable |= found
    assert set(schemas) == reachable
    assert trimmed["components"]["securitySchemes"] == full["components"]["securitySchemes"]


def test_packaged_spec_matches_script_output():
    # A pin bump must regenerate the packaged spec with the script.
    mod = _script()
    packaged = resources.files("fabri.tools.connector_catalog").joinpath("specs/cakemail.json").read_text(
        encoding="utf-8")
    assert packaged == mod.dump(mod.trim(_full()))


def test_packaged_spec_loads_as_resource():
    text = resources.files("fabri.tools.connector_catalog").joinpath("specs/cakemail.json").read_text(
        encoding="utf-8")
    doc = json.loads(text)
    assert str(doc["openapi"]).startswith("3")
    assert spec_text("cakemail") == text
    ids = {op["operationId"] for _, _, op in _ops(doc)}
    assert set(cakemail.CURATED) <= ids


def test_deny_list_against_full_spec_by_path():
    trimmed = _script().trim(_full())
    families = re.compile(r"token|pat_|_pat|pats|user|account|mfa|password|dkim|export|action|form|"
                          r"workflow|webhook", re.IGNORECASE)
    senders = set()
    for method, path, op in _ops(trimmed):
        op_id = op["operationId"]
        assert not path.startswith(("/token", "/users", "/accounts")), path
        assert "pats" not in path, path
        if path.startswith("/brands"):
            assert (method, path) in cakemail.KEEP_UNDER_DROPPED, (method, path)
        # "transactional" holds the letters of "action"; that family is the
        # workflow actions, matched case-sensitively below.
        word = op_id.replace("Transactional", "")
        assert not families.search(word), op_id
        if "sender" in op_id.lower():
            senders.add(op_id)
    assert senders == {"listSenders", "getSender"}
    full_ids = {op["operationId"] for _, _, op in _ops(_full())}
    for gone in ("createToken", "createTokenFromChallenge", "listUsers", "getSelfAccount", "createMfa",
                 "resetSelfPassword", "create_pat_endpoint_users_self_pats_post", "createSender", "patchSender",
                 "confirmSender", "resendConfirmationEmail", "exportContacts", "createAction", "sendTestAction",
                 "submitForm", "createWebhook", "createWorkflow", "patchDomains", "patchSystemEmails",
                 "list_dkim_keys_brands_default_dkim_get"):
        assert gone in full_ids
        assert gone not in {op["operationId"] for _, _, op in _ops(trimmed)}, gone


# ---------------------------------------------------------------- the expansion


def test_entry_and_expansion_shape():
    entry = CONNECTORS["cakemail"]
    assert entry.token_env == "CAKEMAIL_TOKEN" and entry.secret_provider == "cakemail"
    assert entry.spec_file == "cakemail.json" and entry.verify_path == "/users/self"
    assert len(entry.default_allow) == 28
    data = connector_entry("cakemail")
    source = data["sources"][0]
    assert data["allow"] == list(entry.default_allow)
    assert source["spec"] == "specs/cakemail.json" and source["base_url"] == BASE
    assert source["methods"] == ["GET", "POST", "PATCH"]
    assert source["approval_mode"] == "tighten" and source["catalog"] == "cakemail"
    assert source["headers"] == {"Authorization": {"env": "CAKEMAIL_TOKEN", "prefix": "Bearer "}}
    assert len(source["approval"]) == 7 and set(source["approval"].values()) == {"always_approve"}
    assert set(source["names"]) == set(cakemail.CURATED)
    ws = connector_entry("cakemail", name="news", workspace="acme")["sources"][0]
    assert ws["headers"]["Authorization"] == {"secret_ref": "cakemail:acme", "prefix": "Bearer "}
    assert ws["spec"] == "specs/news.json" and all(n.startswith("news_") for n in ws["names"].values())


def test_expand_refuses_a_spec_missing_a_curated_operation(monkeypatch):
    real = cakemail._spec_ops()
    monkeypatch.setattr(cakemail, "_spec_ops", lambda: tuple(o for o in real if o[0] != "scheduleCampaign"))
    cakemail._tool_names.cache_clear()
    try:
        with pytest.raises(ConnectorError, match="scheduleCampaign"):
            connector_entry("cakemail", name="other")
    finally:
        cakemail._tool_names.cache_clear()


def test_minimum_and_request_floor():
    assert cakemail.minimum(LIST_LISTS, "GET") is None
    for method in ("POST", "PATCH", "PUT", "DELETE", None):
        assert cakemail.minimum(LIST_LISTS, method) == "always_approve"
    doc = _packaged()
    by_id = {op["operationId"]: (method, path) for method, path, op in _ops(doc)}
    for op_id in cakemail.CURATED_READS:
        method, path = by_id[op_id]
        assert request_floor(BASE + path, method) == ("cakemail", None), op_id
    curated_paths = {by_id[i] for i in cakemail.CURATED_READS}
    for method, path, op in _ops(_full()):
        if (method, path) not in curated_paths:
            assert request_floor(BASE + path, method) == ("cakemail", "always_approve"), (method, path)
    for url in (BASE + "/lists/../users/self", BASE + "/lists/%2e%2e", BASE + "/lists/", BASE + "/users/self",
                "https://API.cakemail.dev./tags", "https://eu.api.cakemail.dev/tags"):
        method = "POST" if url.endswith("/tags") else "GET"
        assert request_floor(url, method) == ("cakemail", "always_approve"), url
    assert request_floor("https://api.cakemail.dev/lists/123/contacts/9", "GET") == ("cakemail", None)


# ---------------------------------------------------------------- the run path


def test_curated_build_through_toolset(tmp_path):
    tools = _resolve(_layer(tmp_path, connector_entry("cakemail"))).tools
    assert sorted(tools) == sorted(CONNECTORS["cakemail"].default_allow)
    assert len(tools) == 28 <= 40
    for tool in tools.values():
        expected = "read" if tool.method == "GET" else "always_approve"
        assert tool.approval == expected, (tool.name, tool.method, tool.approval)
    assert sum(t.method != "GET" for t in tools.values()) == 7
    assert not approval_warnings(_resolve(_layer(tmp_path / "b", connector_entry("cakemail"))))


def _edited(step: str) -> tuple[dict, dict]:
    entry = connector_entry("cakemail")
    source = entry["sources"][0]
    spec = _packaged()
    if step == "map_read":
        source["approval"][SEND_TEST] = "read"
        source["approval"][SCHEDULE] = "read"
    elif step == "map_removed":
        del source["approval"]
    elif step == "tighten_removed":
        source["approval"] = {SEND_TEST: "read", SCHEDULE: "read"}
        del source["approval_mode"]
    elif step == "hint_removed":
        for _, _, op in _ops(spec):
            op.pop("x-fabri-approval", None)
    elif step == "catalog_removed":
        source["approval"] = {SEND_TEST: "read", SCHEDULE: "read"}
        del source["catalog"]
    elif step == "all_removed":
        source["approval"] = {SEND_TEST: "read", SCHEDULE: "read"}
        del source["approval_mode"], source["catalog"]
        for _, _, op in _ops(spec):
            op.pop("x-fabri-approval", None)
    return entry, spec


@pytest.mark.parametrize("step", ["map_read", "map_removed", "tighten_removed", "hint_removed",
                                  "catalog_removed", "all_removed"])
def test_written_yaml_edits_cannot_lower_a_write(tmp_path, step):
    entry, spec = _edited(step)
    tools = _resolve(_layer(tmp_path, entry, spec)).tools
    for name in (SEND_TEST, SCHEDULE):
        assert tools[name].approval == "always_approve", (step, name)
    assert tools[LIST_LISTS].approval == "read"


def test_markers_removed_every_unlisted_call_is_always_approve(tmp_path):
    # Every operation exposed, every class written as read, both markers gone,
    # the spec hints stripped and one write moved to GET in the spec: only the
    # 21 curated GET paths stay read.
    entry = connector_entry("cakemail", all_tools=True)
    source = entry["sources"][0]
    del source["approval_mode"], source["catalog"]
    spec = _packaged()
    for _, _, op in _ops(spec):
        op.pop("x-fabri-approval", None)
    item = spec["paths"]["/campaigns/{campaign_id}/send-test"]
    item["get"] = item.pop("post")
    source["approval"] = {tool: "read" for tool in source["names"].values()}
    entry["sources"].append({"kind": "http", "name": "cakemail_me", "url": BASE + "/users/self", "method": "GET",
                             "approval": "read"})
    entry["sources"].append({"kind": "http", "name": "cakemail_peek", "url": BASE + "/lists/../users/self",
                             "method": "GET", "approval": "read"})
    tools = _resolve(_layer(tmp_path, entry, spec)).tools
    curated_reads = {source["names"][i] for i in cakemail.CURATED_READS}
    assert len(tools) == len(source["names"]) + 2
    assert tools[SEND_TEST].method == "GET"
    for name, tool in tools.items():
        expected = "read" if name in curated_reads else "always_approve"
        assert tool.approval == expected, (name, tool.method, tool.path)


def test_hand_written_source_on_another_host_is_on_its_own(tmp_path):
    # Pointed at a host the catalog does not own, with every marker removed,
    # nothing holds the class up; approval_warnings is what flags it.
    entry, spec = _edited("all_removed")
    source = entry["sources"][0]
    source["base_url"] = "https://mail-proxy.example.test"
    source["approval"][CONTACT_UPDATE] = "read"
    tool_set = _resolve(_layer(tmp_path, entry, spec))
    for name in (SEND_TEST, SCHEDULE, CONTACT_UPDATE):
        assert tool_set.tools[name].approval == "read", name
    named = {tool.name for tool, _ in approval_warnings(tool_set)}
    assert {SEND_TEST, SCHEDULE, CONTACT_UPDATE} <= named
    # With the map gone too, only fabri's own name and method classifier is left.
    del source["approval"]
    plain = _resolve(_layer(tmp_path / "b", entry, spec)).tools
    assert plain[CONTACT_UPDATE].approval == "write"
    assert plain[SEND_TEST].approval == "always_approve"  # "send" in the name, not the catalog


def test_all_tools_widens_and_stays_floored(tmp_path):
    entry = connector_entry("cakemail", all_tools=True)
    source = entry["sources"][0]
    assert entry["allow"] == ["cakemail_*"]
    ops = list(_ops(_packaged()))
    assert len(source["names"]) == len(ops)
    assert len(source["approval"]) == sum(m != "GET" for m, _, _ in ops)
    tools = _resolve(_layer(tmp_path, entry)).tools
    # The build-time deny list agrees with the trim: every packaged operation builds.
    assert sorted(tools) == sorted(source["names"].values())
    curated_reads = {source["names"][i] for i in cakemail.CURATED_READS}
    for name, tool in tools.items():
        if tool.method != "GET" or name not in curated_reads:
            assert tool.approval == "always_approve", name
        else:
            assert tool.approval == "read", name
    assert tools["cakemail_email_create"].approval == "always_approve"  # POST /emails sends mail


def test_mock_transport_read_allowed_write_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CAKEMAIL_TOKEN", "x")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [], "id": 7})

    tool_set = _resolve(_layer(tmp_path, connector_entry("cakemail")), transport=httpx.MockTransport(handle))
    registry = tool_set.registry()
    assert registry.approver is None

    result = registry.invoke(LIST_LISTS, {})
    assert result["ok"] is True, result
    assert len(seen) == 1 and seen[0].method == "GET" and seen[0].url.path == "/lists"
    assert seen[0].url.host == "api.cakemail.dev" and seen[0].headers["Authorization"] == "Bearer x"

    create = "cakemail_campaign_create"
    result = registry.invoke(create, {"name": "Autumn"})
    assert result["ok"] is False and "no approver is configured" in result["error"]
    assert len(seen) == 1

    asked: list[str] = []
    registry.approver = lambda name, args, manifest: asked.append(name) or False
    result = registry.invoke(create, {"name": "Autumn"})
    assert result["ok"] is False and "declined" in result["error"]
    assert asked == [create] and len(seen) == 1

    registry.approver = lambda *a: True
    result = registry.invoke(create, {"name": "Autumn"})
    assert result["ok"] is True, result
    assert len(seen) == 2 and seen[1].method == "POST" and seen[1].url.path == "/campaigns"
    assert json.loads(seen[1].content) == {"name": "Autumn"}


def test_missing_credential_is_a_warning_at_add_time(tmp_path, monkeypatch):
    monkeypatch.delenv("CAKEMAIL_TOKEN", raising=False)
    layer = tmp_path / "company"
    (layer / "tools").mkdir(parents=True)
    report = add_entry(layer, connector_tool_entry("cakemail"), policy=ToolPolicy.permissive())
    assert report.written and not report.errors, report.errors
    assert not [f for f in report.lint if f["severity"] == "error"], report.lint
    assert len(report.tools) == 28
    assert (layer / "tools" / "specs" / "cakemail.json").read_text(encoding="utf-8") == spec_text("cakemail")
    written = yaml.safe_load((layer / "tools" / "sources" / "cakemail.yaml").read_text(encoding="utf-8"))
    assert written["sources"][0]["headers"]["Authorization"] == {"env": "CAKEMAIL_TOKEN", "prefix": "Bearer "}
    # The call itself fails cleanly when the token is still missing at run time.
    tools = resolve_tools(resolve_setup([layer]), use_cache=False, catalog=False,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    result = tools.registry().invoke(LIST_LISTS, {})
    assert result["ok"] is False and "CAKEMAIL_TOKEN" in result["error"]
