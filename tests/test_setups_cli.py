"""CLI: fabri setup show|import|lint and fabri tools list|sync|register|..."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from fabri.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def run(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def pet_layer(tmp_path):
    layer = tmp_path / "pets"
    (layer / "tools").mkdir(parents=True)
    shutil.copy(FIXTURES / "petstore.yaml", layer / "tools" / "petstore.yaml")
    (layer / "tools" / "tools.yaml").write_text(
        "sources:\n  - kind: openapi\n    name: pets\n    spec: petstore.yaml\nallow: ['*']\n")
    (layer / "agents" / "chat").mkdir(parents=True)
    (layer / "agents" / "chat" / "agent.yaml").write_text("tools: [pet_list, owner_list]\n")
    return layer


def test_tools_list(monkeypatch, capsys, pet_layer):
    code, out, _ = run(monkeypatch, capsys, "tools", "list", "--layers", str(pet_layer))
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "APPROVAL", "SOURCE", "ENDPOINT", "LAYER", "TITLE"]
    names = [line.split()[0] for line in lines[1:]]
    assert names[:5] == ["owner_list", "pet_adopt", "pet_create", "pet_get", "pet_list"]
    assert "pet_delete" not in names
    code, out, _ = run(monkeypatch, capsys, "tools", "list", "--layers", str(pet_layer), "--type", "chat")
    assert [line.split()[0] for line in out.splitlines()[1:]] == ["owner_list", "pet_list"]


def test_tools_sync_diff(monkeypatch, capsys, pet_layer, tmp_path):
    allow = tmp_path / "allow.yaml"
    allow.write_text("[pet_list, pet_get]\n")
    code, out, _ = run(monkeypatch, capsys, "tools", "sync", "--layers", str(pet_layer), "--global-allow", str(allow))
    assert code == 0 and "added    pet_get" in out and "2 added, 0 removed, 0 changed, 2 total" in out
    assert (pet_layer / "tools/.fabri-state/catalog.json").is_file()
    allow.write_text("[pet_list, owner_list]\n")
    (pet_layer / "tools" / "descriptions.yaml").write_text("pet_list:\n  title: Browse pets\n")
    code, out, _ = run(monkeypatch, capsys, "tools", "sync", "--layers", str(pet_layer), "--global-allow", str(allow))
    assert "added    owner_list" in out and "removed  pet_get" in out and "changed  pet_list" in out
    code, out, _ = run(monkeypatch, capsys, "tools", "sync", "--layers", str(pet_layer), "--global-allow", str(allow))
    assert "0 added, 0 removed, 0 changed, 2 total" in out


def test_setup_show_and_lint(monkeypatch, capsys, pet_layer):
    code, out, _ = run(monkeypatch, capsys, "setup", "show", "--layers", f"base={pet_layer}", "--type", "chat")
    data = json.loads(out)
    assert data["agent"]["type"] == "chat" and data["agent"]["source"] == "base"
    assert data["tools_for_type"] == ["pet_list", "owner_list"]
    code, out, _ = run(monkeypatch, capsys, "setup", "lint", "--layers", str(pet_layer))
    assert code == 0 and "0 errors" in out


def test_setup_import(monkeypatch, capsys, tmp_path):
    src = tmp_path / "src" / "my_skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: My skill\ndescription: Does a thing.\n---\nBody.\n")
    into = tmp_path / "layer"
    code, out, _ = run(monkeypatch, capsys, "setup", "import", "--from", str(src.parent), "--format", "skill-md",
                       "--into", str(into), "--dry-run")
    assert json.loads(out)["created"] == ["my-skill"] and not into.exists()
    run(monkeypatch, capsys, "setup", "import", "--from", str(src.parent), "--into", str(into))
    code, out, _ = run(monkeypatch, capsys, "setup", "import", "--from", str(src.parent), "--into", str(into))
    assert json.loads(out)["skipped"] == ["my-skill"] and code == 0


def test_register_cli(monkeypatch, capsys, tmp_path):
    layer = tmp_path / "proj"
    layer.mkdir()
    decl = tmp_path / "decl.json"
    decl.write_text(json.dumps({"name": "crm_lookup", "kind": "http", "description": "Look up.",
                                "endpoint": "https://api.crm.test/c/{id}", "input_schema": {
                                    "type": "object", "properties": {"id": {"type": "string"}}}}))
    policy = tmp_path / "policy.yaml"
    policy.write_text("allow: [crm_*]\nallowed_kinds: [http]\nallowed_hosts: [api.crm.test]\n")
    code, out, _ = run(monkeypatch, capsys, "tools", "register", "--layer", str(layer), "--file", str(decl),
                       "--policy", str(policy), "--actor", "alice")
    assert code == 0 and out.startswith("registered crm_lookup version 1 (http, read)")
    code, out, _ = run(monkeypatch, capsys, "tools", "list", "--layers", str(layer))
    assert "crm_lookup" in out and "registered" in out
    code, out, _ = run(monkeypatch, capsys, "tools", "disable", "--layer", str(layer), "--name", "crm_lookup")
    assert out.strip() == "disabled crm_lookup"
    code, out, _ = run(monkeypatch, capsys, "tools", "list", "--layers", str(layer))
    assert "crm_lookup" not in out
    run(monkeypatch, capsys, "tools", "enable", "--layer", str(layer), "--name", "crm_lookup")
    code, out, _ = run(monkeypatch, capsys, "tools", "unregister", "--layer", str(layer), "--name", "crm_lookup")
    assert out.strip() == "unregistered crm_lookup"
    policy.write_text("allow: [crm_*]\nallowed_kinds: [http]\nallowed_hosts: [other.test]\n")
    code, _, err = run(monkeypatch, capsys, "tools", "register", "--layer", str(layer), "--file", str(decl),
                       "--policy", str(policy))
    assert code == 1 and err.startswith("refused:")


def test_bare_tools_still_lists_builtins(monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "tools", "--search", "read_file")
    assert code == 0 and "read_file" in out
