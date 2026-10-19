"""fabri.tools.catalog, probe and describe: names, titles, descriptions,
retrieval, find_tools/describe_tool, probing and LLM rewrites. MockTransport
only, scripted LLM backends only."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from fabri.core.llm import LLMResponse, ScriptedLLMBackend, ToolCall
from fabri.memory.embeddings import embeddings_available
from fabri.setups import build_agent_config, resolve_setup, resolve_tools
from fabri.tools.catalog import DESCRIPTION_CAP, TITLE_CAP, Catalog
from fabri.tools.describe import describe_with_llm, grounding_errors
from fabri.tools.probe import capture_options, probe_tools, sanitize

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def layers(tmp_path):
    base, project = tmp_path / "base", tmp_path / "project"
    shutil.copy(FIXTURES / "petstore.yaml", tmp_path / "petstore.yaml")
    shutil.copy(FIXTURES / "clinic.json", tmp_path / "clinic.json")
    write(base / "tools/tools.yaml", """
sources:
  - kind: openapi
    name: pets
    spec: ../../petstore.yaml
    dry_run_header: {name: X-Source-Dry, value: "1"}
  - kind: openapi
    name: clinic
    spec: ../../clinic.json
    strip_prefixes: [admin]
allow: ["*"]
""")
    project.mkdir()
    return [("base", base), ("project", project)]


class Api:
    """A toy petstore and clinic behind MockTransport."""

    def __init__(self):
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/pets" and request.method == "GET":
            return httpx.Response(200, json=[
                {"id": 1, "name": "Rex", "status": "available", "owner_email": "a@b.test", "token": "zzz"},
                {"id": 2, "name": "Tom", "status": "adopted", "phone": "+1 555 123 4567"},
                {"id": 3, "name": "Kit", "status": "available", "note": "call +1 (555) 987-6543"}])
        if path.startswith("/pets/") and request.method == "GET":
            return httpx.Response(200, json={"id": int(path.rsplit("/", 1)[-1]), "name": "Rex"})
        if path == "/v1/owners":
            return httpx.Response(200, json={"data": [{"id": 10, "slug": "ann", "email": "ann@x.test"}]})
        if path.endswith("/adopt"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def test_titles_and_descriptions(layers):
    tools = resolve_tools(layers, transport=Api().transport())
    cat: Catalog = tools.catalog
    adopt = cat.entries["pet_adopt"]
    assert adopt.title == "Adopt a pet" and adopt.title_source == "schema"
    lines = adopt.description.splitlines()
    assert lines[0] == "Adopt a pet. Marks the pet as adopted by the given owner."
    assert "use pet_update to update a pet" in lines[1] and "use pet_create to create a pet" in lines[1]
    assert lines[2] == "Params: owner_id (integer, required); petId (integer, required)."
    assert lines[3].startswith("Errors: 409 (duplicate_name|already_adopted|not_available) conflict: re-read")
    assert lines[-1] == "Approval: write."
    assert cat.entries["pet_list"].description.count("status (string) one of available|adopted") == 1
    # clinic op without a short summary falls back to "<Action> a <resource>"
    replace = cat.entries["appointment_replace"]
    assert replace.title == "Replace an appointment"
    assert "409 (slot_taken|clinic_closed)" in replace.description
    for entry in cat.entries.values():
        assert len(entry.title) <= TITLE_CAP and len(entry.description) <= DESCRIPTION_CAP
    # the manifest carries the catalog text, so retrieval and the model see it
    assert tools.get("pet_adopt").manifest.description == adopt.description


def test_title_built_from_resource_when_no_summary(layers, tmp_path):
    doc = json.loads((tmp_path / "clinic.json").read_text())
    del doc["paths"]["/admin/v2/categories"]["get"]["summary"]
    (tmp_path / "clinic.json").write_text(json.dumps(doc))
    cat = resolve_tools(layers, transport=Api().transport()).catalog
    assert cat.entries["category_list"].title == "List categories"


def test_catalog_entry_fields(layers):
    entry = resolve_tools(layers, transport=Api().transport()).catalog.entries["pet_get"]
    data = entry.to_dict()
    assert {k: data[k] for k in ("name", "alias", "resource", "action", "method", "path", "tags", "approval",
                                 "source", "kind")} == {
        "name": "pet_get", "alias": ["showPetById"], "resource": "pet", "action": "get", "method": "GET",
        "path": "/pets/{petId}", "tags": ["pets"], "approval": "read", "source": "pets", "kind": "openapi"}
    assert data["params"] == [{"name": "petId", "type": "integer", "required": True, "enum": None,
                               "options": None, "location": "path"}]


def test_alias_resolution(layers):
    api = Api()
    tools = resolve_tools(layers, transport=api.transport())
    registry = tools.registry()
    assert registry.canonical_name("adoptPet") == "pet_adopt"
    assert registry.invoke("adoptPet", {"petId": 4, "owner_id": 1})["ok"]
    assert api.calls[-1].url.path == "/pets/4/adopt"
    assert tools.catalog.get("Appointments_Book").name == "clinic_appointment_create"
    assert tools.approvals()["Appointments_Book"] == "write"


def test_layer_description_override_wins(layers):
    write(layers[1][1] / "tools/descriptions.yaml", "pet_list:\n  title: Browse pets\n  description: Lists pets.\n")
    entry = resolve_tools(layers, transport=Api().transport()).catalog.entries["pet_list"]
    assert entry.title == "Browse pets" and entry.description == "Lists pets."
    assert entry.title_source == "layer:project" and entry.description_source == "layer:project"


@pytest.mark.skipif(not embeddings_available(), reason="needs fabri[embeddings]")
@pytest.mark.parametrize("query,expected", [
    ("adopt a pet for an owner", "pet_adopt"),
    ("book a new appointment slot at the clinic", "clinic_appointment_create"),
    ("list all owners", "owner_list"),
])
def test_top_k_retrieval_picks_the_right_tools(layers, query, expected):
    from fabri.orchestrator.retrieval import retrieve_tools

    tools = resolve_tools(layers, transport=Api().transport())
    picked = [m.name for m in retrieve_tools(query, tools.registry(), top_k=2)]
    assert expected in picked
    assert expected in [e.name for e in tools.catalog.search(query, 2)]


def test_lexical_search_fallback(layers, monkeypatch):
    from fabri.tools import catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "_embedding_scores", lambda q, pool: None)
    cat = resolve_tools(layers, transport=Api().transport()).catalog
    assert cat.search("adopt pet", 1)[0].name == "pet_adopt"
    assert cat.search("appointments booking", 1)[0].name == "clinic_appointment_create"


class RecordingBackend(ScriptedLLMBackend):
    def __init__(self, script):
        super().__init__(script)
        self.tool_lists: list[list[str]] = []

    def set_tools(self, tool_defs):
        self.tool_lists.append([t["name"] for t in tool_defs])


def test_find_and_describe_end_to_end(layers, monkeypatch):
    from fabri import QdrantMemoryStore, run_agent
    from fabri.core import agent as agent_mod

    events: list[dict] = []
    real_log = agent_mod.log_event
    monkeypatch.setattr(agent_mod, "log_event", lambda sid, ev: (events.append(ev), real_log(sid, ev)))
    api = Api()
    resolved = resolve_setup(layers)
    tools = resolve_tools(resolved, transport=api.transport())
    write(layers[1][1] / "agents/helper/agent.yaml", "max_steps: 6\n")
    cfg, registry = build_agent_config(resolve_setup(layers), "helper", {"provider": "openai", "model": "gpt-5-mini"},
                                       tool_set=tools)
    assert {"find_tools", "describe_tool", "pet_adopt"} <= set(registry.tools)
    backend = RecordingBackend([
        LLMResponse(tool_call=ToolCall(name="find_tools", args={"query": "adopt a pet", "k": 2}, id="t1")),
        LLMResponse(tool_call=ToolCall(name="describe_tool", args={"name": "pet_adopt"}, id="t2")),
        LLMResponse(tool_call=ToolCall(name="adoptPet", args={"petId": 1, "owner_id": 10}, id="t3")),
        LLMResponse(final_text="Pet 1 is adopted."),
    ])
    store = QdrantMemoryStore(collection=f"catalog_{uuid.uuid4().hex[:10]}")
    result = run_agent("list all owners", backend, registry, store, tool_retrieval_enabled=True,
                       tool_retrieval_top_k=1, tool_retrieval_always_include=("find_tools", "describe_tool"),
                       max_steps=6)
    assert result["outcome"] == "success"
    assert api.calls[-1].method == "POST" and api.calls[-1].url.path == "/pets/1/adopt"
    calls = [e for e in events if e.get("type") == "tool_call"]
    found = calls[0]["result"]["result"]["tools"]
    assert found[0]["name"] == "pet_adopt" and found[0]["title"] == "Adopt a pet"
    described = calls[1]["result"]["result"]
    assert described["input_schema"]["required"] == ["petId", "owner_id"]
    alias_call = calls[2]
    assert alias_call["name"] == "adoptPet" and alias_call["canonical_name"] == "pet_adopt"
    if embeddings_available():
        assert "pet_adopt" not in backend.tool_lists[0]
        assert "pet_adopt" in backend.tool_lists[-1]
        assert any(e.get("type") == "tools_exposed" for e in events)


def test_probe_only_gets_with_dry_run_and_redaction(layers):
    api = Api()
    report = probe_tools(layers, transport=api.transport())
    assert api.calls and all(r.method in ("GET", "OPTIONS") for r in api.calls)
    assert all(r.headers.get("X-Dry-Run") == "true" for r in api.calls)
    assert all(r.headers.get("X-Source-Dry") == "1" for r in api.calls if r.url.host == "petstore.test")
    assert "pet_list" in report["probed"] and "pet_get" in report["probed"]
    assert report["skipped"]["pet_create"].startswith("POST")
    state = json.loads((layers[1][1] / "tools/.fabri-state/probe.json").read_text())
    raw = json.dumps(state)
    for secret in ("a@b.test", "zzz", "555 123 4567", "ann@x.test", "987-6543"):
        assert secret not in raw
    pet_list = state["tools"]["pet_list"]
    assert pet_list["probed_at"].endswith("Z")
    assert pet_list["captured"]["id"] == [1, 2, 3] and pet_list["captured"]["status"] == ["available", "adopted"]
    # ids learned from the list op become options for the write op's path param
    assert state["tools"]["pet_adopt"]["options"] == {"petId": [1, 2, 3], "owner_id": [10]}
    assert state["tools"]["owner_list"]["captured"]["slug"] == ["ann"]
    # pet_get was called with the learned ids, bounded by max_calls_per_op
    assert [r.url.path for r in api.calls if r.url.path.startswith("/pets/")] == ["/pets/1", "/pets/2", "/pets/3"]
    entry = resolve_tools(layers, transport=api.transport()).catalog.entries["pet_adopt"]
    assert entry.options["petId"] == [1, 2, 3] and "petId (integer, required) e.g. 1, 2, 3" in entry.description


def test_probe_never_sends_non_get_even_when_asked(layers):
    api = Api()
    report = probe_tools(layers, transport=api.transport(), ops=["pet_adopt", "createPet"], write=False)
    assert api.calls == [] and set(report["skipped"]) == {"pet_adopt", "pet_create"}


def test_probe_safe_extension_allows_marked_op(layers, tmp_path):
    doc_path = tmp_path / "petstore.yaml"
    doc_path.write_text(doc_path.read_text().replace("      operationId: adoptPet\n",
                                                     "      operationId: adoptPet\n      x-fabri-probe-safe: true\n"))
    api = Api()
    probe_tools(layers, transport=api.transport(), max_calls_per_op=1)
    adopt = [r for r in api.calls if r.url.path.endswith("/adopt")]
    # marked probe-safe, so it runs once with a learned pet id and owner id
    assert [(r.method, r.url.path, json.loads(r.content)) for r in adopt] == [("POST", "/pets/1/adopt", {"owner_id": 10})]
    assert all(r.headers["X-Dry-Run"] == "true" for r in api.calls)
    assert not [r for r in api.calls if r.method not in ("GET", "OPTIONS") and not r.url.path.endswith("/adopt")]


def test_sanitize_and_capture():
    data = {"user": {"email": "x@y.test", "Phone": "123", "api_key": "k", "bio": "x" * 100,
                     "contact": "mail me at q@r.test"}}
    clean = sanitize(data)
    assert clean["user"]["email"] == "[redacted]" and clean["user"]["Phone"] == "[redacted]"
    assert clean["user"]["api_key"] == "[redacted]" and clean["user"]["contact"] == "[redacted]"
    assert len(clean["user"]["bio"]) == 67
    opts = capture_options([{"id": 1, "kind": "a", "secret": "s"}, {"id": 2, "kind": "a"}])
    assert opts == {"id": [1, 2], "kind": ["a"]}


# ---------------------------------------------------------------- LLM describe


def reply(title, description):
    return LLMResponse(final_text=json.dumps({"title": title, "description": description}))


class CountingBackend(ScriptedLLMBackend):
    def __init__(self, script):
        super().__init__(script)
        self.calls = 0

    def step(self, system, messages):
        self.calls += 1
        assert "Do not invent parameters" in system
        return super().step(system, messages)


def test_llm_describe_grounded_and_cached(layers):
    transport = Api().transport()
    good = CountingBackend([reply("Adopt a pet", "Marks a pet as adopted. Needs petId and owner_id. "
                                                 "On 409 (already_adopted) re-read the pet. Approval: write.")])
    report = describe_with_llm(layers, good, names=["pet_adopt"], transport=transport)
    assert report["written"] == ["pet_adopt"] and good.calls == 1
    entry = resolve_tools(layers, transport=transport).catalog.entries["pet_adopt"]
    assert entry.description_source == "llm" and entry.description.startswith("Marks a pet as adopted.")
    again = CountingBackend([])
    second = describe_with_llm(layers, again, names=["pet_adopt"], transport=transport)
    assert second["cached"] == ["pet_adopt"] and again.calls == 0


def test_llm_describe_rejects_invented_param(layers):
    transport = Api().transport()
    bad = CountingBackend([reply("Adopt a pet", "Pass petId, owner_id and adoption_fee. Returns 201.")])
    report = describe_with_llm(layers, bad, names=["pet_adopt"], transport=transport)
    assert "adoption_fee" in report["fallback"]["pet_adopt"] and "201" in report["fallback"]["pet_adopt"]
    entry = resolve_tools(layers, transport=transport).catalog.entries["pet_adopt"]
    assert entry.description_source == "schema"


def test_llm_describe_slop_check_regenerates_once(layers):
    transport = Api().transport()
    backend = CountingBackend([
        reply("Adopt a pet", "Leverage this robust tool to adopt a pet with petId and owner_id."),
        reply("Adopt a pet", "Adopts a pet. Needs petId and owner_id."),
    ])
    report = describe_with_llm(layers, backend, names=["pet_adopt"], transport=transport)
    assert report["written"] == ["pet_adopt"] and backend.calls == 2
    sloppy = CountingBackend([
        reply("List pets", "Leverage this robust listing."),
        reply("List pets", "A seamless, comprehensive listing."),
    ])
    report = describe_with_llm(layers, sloppy, names=["pet_list"], transport=transport)
    assert report["fallback"]["pet_list"] == "writing tells after one rewrite" and sloppy.calls == 2


def test_grounding_errors():
    facts = {"name": "pet_get", "aliases": ["showPetById"], "sibling_tools": ["pet_list"],
             "params": [{"name": "petId", "enum": None}], "responses": {"200": [], "404": []}}
    assert grounding_errors("Use petId. Try pet_list first. 404 means missing.", facts) == []
    assert grounding_errors("Use pet_uuid. Returns 500.", facts) == [
        "unknown identifier pet_uuid", "undeclared status code 500"]
