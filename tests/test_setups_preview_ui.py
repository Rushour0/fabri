"""The starter setups, the preview endpoint, the ui config block, tool usage
across runs and `fabri setups init`. Runs over real HTTP on loopback against
a copy of the starter; no outside network."""
from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

import httpx
import pytest

import fabri.memory.embeddings as emb
from fabri.service.http_server import serve_http
from fabri.service.service import FabriService
from fabri.service.setups_routes import SetupsApi, check_ui_config
from fabri.setups import SetupError, clear_cache, has_errors, lint_setup, resolve_setup, resolve_tools

STARTER = Path(__file__).parent.parent / "src" / "fabri" / "examples" / "setups" / "starter"


@pytest.fixture
def starter(tmp_path, monkeypatch):
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    # Word-overlap ranking keeps the expected order independent of the model.
    monkeypatch.setattr(emb, "embeddings_available", lambda: False)
    root = tmp_path / "setups"
    shutil.copytree(STARTER, root)
    clear_cache()
    return root


@pytest.fixture
def client(starter, tmp_path):
    api = SetupsApi.from_files(None, starter / "setups.yaml")
    service = FabriService(home_root=tmp_path / "home")
    server = serve_http(service, port=0, setups_api=api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}")
    yield http, service
    server.shutdown()
    server.server_close()
    service.close()


def test_starter_resolves_and_lints_clean(starter):
    layers = [("baseline", starter / "baseline"), ("saas", starter / "packs/saas"),
              ("acme", starter / "companies/acme"), ("mobile-launch", starter / "companies/acme/projects/mobile-launch")]
    resolved = resolve_setup(layers)
    assert set(resolved.agent_types()) == {"support", "triage"}
    tools = resolve_tools(resolved)
    assert not tools.errors
    assert tools.tools["ticket_reply"].manifest.approval == "always_approve"
    findings = lint_setup(resolved, tool_set=tools)
    assert not has_errors(findings), findings
    prompt, warnings = resolved.system_prompt("support", task="the android app does not sync")
    assert warnings == [] and "Acme sells a project tracker" in prompt and "- `mobile-sync`" in prompt


def test_preview_ranks_skills_and_tools(client):
    http, _ = client
    r = http.get("/setups/preview", params={"org": "acme", "project": "mobile-launch", "type": "support",
                                            "task": "android app shows duplicates and does not sync"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ranking"] == "words" and body["skill_top_k"] == 3 and body["tool_top_k"] == 6
    assert {s["name"] for s in body["skills"]["always"]} == {"write-plainly", "acme-voice"}
    on_demand = body["skills"]["on_demand"]
    assert on_demand[0]["name"] == "mobile-sync" and on_demand[0]["full"] is True
    assert [s["full"] for s in on_demand].count(True) == 3
    assert {t["name"] for t in body["tools"]} == {"tickets_search", "ticket_get", "ticket_reply"}
    assert all(t["shown"] for t in body["tools"])
    assert body["prompt"]["chars"] > 0 and body["prompt"]["budgets"]["skills_total_chars"] == 8000


def test_preview_validates_input(client):
    http, _ = client
    assert http.get("/setups/preview", params={"type": "support"}).status_code == 400
    assert http.get("/setups/preview", params={"type": "nope", "task": "x"}).status_code == 404
    assert http.get("/setups/preview", params={"type": "support", "task": "x", "tool_top_k": "0"}).status_code == 400
    r = http.get("/setups/preview", params={"type": "support", "task": "refund", "tool_top_k": "1"})
    assert [t["shown"] for t in r.json()["tools"]].count(True) == 1


def test_ui_config_is_served(client):
    http, _ = client
    ui = http.get("/setups/ui").json()["ui"]
    assert ui["labels"]["workers"] == "Roles" and ui["default_tab"] == "companies"
    assert "preview" in ui["tabs"]


@pytest.mark.parametrize("bad", [
    {"labels": {"nope": "x"}}, {"tabs": ["agents", "agents"]}, {"tabs": []}, {"tabs": ["nope"]},
    {"default_tab": "tools", "tabs": ["agents"]}, {"intro": 3}, {"extra": 1}, {"labels": {"company": ""}},
])
def test_bad_ui_config_is_refused(bad):
    with pytest.raises(SetupError):
        check_ui_config(bad)


def _usage_run(service, session_id: str, usage: dict) -> None:
    trace = service._persisted_trace_path(session_id)
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps({"type": "start", "task": "t"}) + "\n"
                     + json.dumps({"type": "usage", "tool_usage": usage}) + "\n")


def test_tool_usage_sums_recent_runs(client, monkeypatch):
    http, service = client
    runs = [{"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s3"}]
    monkeypatch.setattr(service.run_store, "list_runs", lambda **kw: runs)
    _usage_run(service, "s1", {"ticket_get": {"calls": 2, "failures": 1, "refused": 0, "seconds": 0.5}})
    _usage_run(service, "s2", {"ticket_get": {"calls": 1, "failures": 0, "refused": 0, "seconds": 0.25},
                               "ticket_reply": {"calls": 1, "failures": 0, "refused": 1, "seconds": 0}})
    body = http.get("/runs/tool-usage").json()
    assert body["runs"] == 2
    assert body["tools"]["ticket_get"] == {"calls": 3, "failures": 1, "refused": 0, "seconds": 0.75, "runs": 2}
    assert list(body["tools"]) == ["ticket_get", "ticket_reply"]
    assert http.get("/runs/tool-usage", params={"limit": "0"}).status_code == 400


def test_setups_init_copies_the_starter(tmp_path, monkeypatch, capsys):
    from fabri.cli import main

    target = tmp_path / "mine"
    monkeypatch.setattr(sys, "argv", ["fabri", "setups", "init", str(target)])
    main()
    assert (target / "setups.yaml").is_file() and (target / "companies/acme/projects/mobile-launch").is_dir()
    assert "fabri studio --setups-config" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main()  # refuses a non-empty directory
