"""fabri.tools.openapi: OpenAPI 3 documents become tools. No network: every
HTTP call goes through httpx.MockTransport."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fabri.tools.openapi import (
    OpenAPIError,
    compose_description,
    load_openapi,
    openapi_tool_defs,
    openapi_tools,
    parse_operations,
    readable_parts,
    singular,
)
from fabri.tools.registry import ToolRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
PETSTORE = FIXTURES / "petstore.yaml"
CLINIC = FIXTURES / "clinic.json"


def _recorder(status: int = 200, body=None, calls=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": 1})

    return httpx.MockTransport(handle)


def test_load_from_path_dict_and_url():
    doc = load_openapi(PETSTORE)
    assert doc["info"]["title"] == "Toy petstore"
    assert load_openapi(doc) == doc
    text = PETSTORE.read_text()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=text))
    assert load_openapi("https://petstore.test/openapi.yaml", transport=transport)["paths"]
    with pytest.raises(OpenAPIError):
        load_openapi({"swagger": "2.0"})


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("get", "/pets/", ("pet", "list")),
        ("get", "/pets/{id}/", ("pet", "get")),
        ("post", "/pets/", ("pet", "create")),
        ("patch", "/pets/{id}/", ("pet", "update")),
        ("post", "/pets/{id}/adopt/", ("pet", "adopt")),
        ("get", "/api/v1/pets/{id}/photos", ("pet_photo", "list")),
        ("get", "/admin/categories", ("category", "list")),
    ],
)
def test_readable_parts(method, path, expected):
    assert readable_parts(method, path, strip_prefixes=["api", "admin"]) == expected


def test_singular_keeps_non_plurals():
    assert singular("status") == "status"
    assert singular("categories") == "category"
    assert singular("boxes") == "box"


def test_petstore_names_aliases_and_collision():
    ops = {op.name: op for op in parse_operations(load_openapi(PETSTORE))}
    assert {"pet_list", "pet_create", "pet_get", "pet_update", "pet_delete", "pet_adopt", "owner_list"} <= set(ops)
    assert ops["pet_get"].aliases == ["showPetById"]
    # GET /store/pets also derives pet_list; it gets a stable suffix.
    store = [op for op in ops.values() if op.operation_id == "listStorePets"][0]
    assert store.name.startswith("pet_list_") and len(store.name) == len("pet_list_") + 4
    again = [op for op in parse_operations(load_openapi(PETSTORE)) if op.operation_id == "listStorePets"][0]
    assert again.name == store.name


def test_operation_id_naming_and_override():
    ops = parse_operations(load_openapi(PETSTORE), naming="operation_id", names={"adoptPet": "pet_take_home"})
    names = {op.name for op in ops}
    assert "listPets" in names and "pet_take_home" in names
    assert [op.aliases for op in ops if op.name == "listPets"] == [["pet_list"]]


def test_delete_excluded_unless_explicit():
    doc = load_openapi(PETSTORE)
    assert "pet_delete" not in {op.name for op in openapi_tool_defs(doc)}
    assert "pet_delete" not in {op.name for op in openapi_tool_defs(doc, allow=["pet_*"])}
    assert "pet_delete" in {op.name for op in openapi_tool_defs(doc, allow=["pet_delete"])}
    assert "pet_delete" in {op.name for op in openapi_tool_defs(doc, methods=["GET", "DELETE"])}
    assert "pet_delete" not in {op.name for op in openapi_tool_defs(doc, allow=["pet_delete"], allow_delete=False)}


def test_filters_methods_tags_deny():
    doc = load_openapi(PETSTORE)
    assert {op.method for op in openapi_tool_defs(doc, methods=["GET"])} == {"GET"}
    assert {op.name for op in openapi_tool_defs(doc, tags=["owners"])} == {"owner_list"}
    names = {op.name for op in openapi_tool_defs(doc, deny=["pet_*"])}
    assert names == {"owner_list"}
    # allow matches the operation_id too
    assert {op.name for op in openapi_tool_defs(doc, allow=["listOwners"])} == {"owner_list"}


def test_input_schema_merges_params_and_body():
    ops = {op.name: op for op in parse_operations(load_openapi(PETSTORE))}
    adopt = ops["pet_adopt"].input_schema
    assert set(adopt["properties"]) == {"petId", "owner_id"}
    assert adopt["required"] == ["petId", "owner_id"]
    create = ops["pet_create"].input_schema
    assert create["required"] == ["name"]
    assert create["properties"]["tag"]["type"] == ["string", "null"]  # nullable resolved
    listing = ops["pet_list"].input_schema
    assert listing["properties"]["status"]["enum"] == ["available", "adopted"]
    assert "required" not in listing


def test_ref_heavy_document():
    ops = {op.operation_id: op for op in parse_operations(load_openapi(CLINIC), strip_prefixes=["admin"])}
    book = ops["Appointments_Book"]
    props = book.input_schema["properties"]
    # $ref parameter, allOf body flattened, oneOf kept, nullable enum gets None
    assert props["clinicId"]["pattern"] == "^[a-z0-9-]+$"
    assert {"patient", "notes", "slot", "contact", "priority"} <= set(props)
    assert len(props["contact"]["oneOf"]) == 2
    assert props["priority"]["enum"] == ["low", "high", None]
    assert book.input_schema["required"] == ["clinicId", "patient", "slot"]
    assert "slot_taken" in book.description and "Responses: 201, 409 (reason: slot_taken|clinic_closed), 422." in book.description
    # header parameters are host business, never model input
    assert "X-Trace" not in json.dumps(book.input_schema)
    # a recursive schema terminates
    tree = ops["Categories_Tree"]
    assert tree.name == "category_list"
    assert ops["Appointments_List"].name == "clinic_appointment_list"


def test_description_cap_keeps_responses_line():
    responses = {"200": {"reasons": []}, "409": {"reasons": ["taken"]}}
    text = compose_description("Sum", "x" * 5000, responses, "GET /", cap=1500)
    assert len(text) <= 1500
    assert text.endswith("Responses: 200, 409 (reason: taken).")


def test_approval_defaults_and_overrides():
    doc = load_openapi(PETSTORE)
    ops = {op.name: op for op in parse_operations(doc, approval={"createPet": "always_approve"})}
    assert ops["pet_list"].approval == "read"
    assert ops["pet_update"].approval == "write"
    assert ops["pet_create"].approval == "always_approve"
    doc["paths"]["/pets/{petId}/adopt"]["post"]["x-fabri-approval"] = "always_approve"
    assert {op.name: op for op in parse_operations(doc)}["pet_adopt"].approval == "always_approve"


def test_callable_builds_request_and_maps_status(monkeypatch):
    calls: list[httpx.Request] = []
    monkeypatch.setenv("PETS_TOKEN", "s3cret-value")
    pairs = openapi_tools(
        PETSTORE, transport=_recorder(201, {"id": 7}, calls), allow=["pet_*"],
        headers={"Authorization": {"env": "PETS_TOKEN", "prefix": "Bearer "}},
        dry_run_header={"name": "X-Dry-Run", "value": "1"}, dry_run=True,
    )
    registry = ToolRegistry([])
    for manifest, handler in pairs:
        registry.register_callable(manifest, handler)
    out = registry.invoke("pet_adopt", {"petId": 3, "owner_id": 9})
    assert out == {"ok": True, "result": {"status": "ok", "code": 201, "data": {"id": 7}}}
    req = calls[-1]
    assert req.method == "POST" and req.url.path == "/pets/3/adopt"
    assert json.loads(req.content) == {"owner_id": 9}
    assert req.headers["Authorization"] == "Bearer s3cret-value"
    assert req.headers["X-Dry-Run"] == "1"
    # the operation_id alias resolves to the same tool
    registry.invoke("listPets", {"limit": 5, "status": "available"})
    assert calls[-1].url.params["limit"] == "5"


@pytest.mark.parametrize(
    "status,expected",
    [
        (409, {"ok": False, "error": "conflict", "result": {"status": "conflict", "code": 409, "data": {"reason": "already_adopted"}}}),
        (404, {"ok": False, "error": "HTTP 404", "result": {"status": "error", "code": 404, "data": {"reason": "already_adopted"}}}),
        (503, {"ok": False, "error": "HTTP 503", "result": {"status": "error", "code": 503, "data": {"reason": "already_adopted"}}}),
    ],
)
def test_status_mapping(status, expected):
    pairs = dict((m.name, h) for m, h in openapi_tools(PETSTORE, transport=_recorder(status, {"reason": "already_adopted"})))
    assert pairs["pet_adopt"]({"petId": 1, "owner_id": 2}) == expected


def test_missing_secret_never_leaks_and_bad_args(monkeypatch, caplog):
    monkeypatch.delenv("PETS_TOKEN", raising=False)
    pairs = dict((m.name, h) for m, h in openapi_tools(
        PETSTORE, transport=_recorder(), headers={"Authorization": {"env": "PETS_TOKEN"}}))
    out = pairs["pet_list"]({})
    assert out["ok"] is False and "PETS_TOKEN" in out["error"]
    assert pairs["pet_get"]({})["error"].endswith("missing path parameter 'petId'")
    assert "unknown arguments" in pairs["pet_list"]({"nope": 1})["error"]


def test_header_value_not_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("PETS_TOKEN", "never-log-me")
    caplog.set_level(logging.DEBUG)
    pairs = dict((m.name, h) for m, h in openapi_tools(
        PETSTORE, transport=_recorder(), headers={"Authorization": {"env": "PETS_TOKEN"}}))
    pairs["pet_list"]({})
    assert "never-log-me" not in caplog.text


def test_transport_error_maps_to_error():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    pairs = dict((m.name, h) for m, h in openapi_tools(PETSTORE, transport=httpx.MockTransport(boom)))
    out = pairs["pet_list"]({})
    assert out["ok"] is False and out["result"] == {"status": "error", "code": None}
