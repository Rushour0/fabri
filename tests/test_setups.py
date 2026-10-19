"""fabri.setups: layered resolver, tool sets, runtime edits, hot reload,
import, lint and the run_agent integration helper. Two toy products go
through the same path. No network: httpx.MockTransport only."""
from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml

from fabri.setups import (
    Setups,
    SetupError,
    build_agent_config,
    delete_item,
    diff_snapshots,
    has_errors,
    import_skills,
    lint_setup,
    put_item,
    resolve_setup,
    resolve_tools,
    set_disabled,
)
from fabri.tools.policy import ToolPolicy

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def recorder(calls: list | None = None, status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json={"path": request.url.path})

    return httpx.MockTransport(handle)


@pytest.fixture
def stack(tmp_path):
    base, pack, org, project = (tmp_path / n for n in ("base", "pack-a", "org", "project"))
    write(base / "setup.yaml", """
        models: {main: small}
        thresholds: {confidence: 0.5, other: 1}
    """)
    write(base / "agents/chat/agent.yaml", """
        max_steps: 4
        tools: [pet_list]
        skills: []
    """)
    write(base / "agents/chat/prompt.md", "You answer questions.\n")
    write(base / "agents/canvas/agent.yaml", "tools: [pet_get]\n")
    write(base / "agents/canvas/prompt.md", "You edit canvases.\n")
    write(base / "skills/charts/skill.yaml", """
        name: charts
        description: Build a chart.
        types: [canvas]
        tools: [pet_list]
    """)
    write(base / "skills/charts/prompt.md", "Charts: use bars.\n")
    write(base / "skills/greet/skill.yaml", "name: greet\ndescription: Greet.\ntypes: []\n")
    write(base / "skills/greet/prompt.md", "Say hello once.\n")
    write(base / "context/house.md", "House rules.\n")
    write(base / "context/canvas-only.md", "---\ntypes: [canvas]\n---\nCanvas notes.\n")
    write(base / "data/teams.csv", "id,name\n1,a\n")
    write(pack / "skills/charts/skill.yaml", """
        name: charts
        description: Build a chart, pack version.
        types: [canvas]
        tools: [pet_list]
    """)
    write(pack / "skills/charts/prompt.md", "Pack charts: use lines.\n")
    write(pack / "context/house.md", "Pack house rules.\n")
    write(org / "setup.yaml", "thresholds: {confidence: 0.8}\n")
    write(org / "agents/chat/agent.yaml", "tools: [owner_list]\nmax_steps: 6\n")
    write(org / "agents/chat/prompt.append.md", "Be brief.\n")
    write(org / "agents/fundraising/agent.yaml", """
        tools: [pet_list]
        route:
          description: Questions about donors and pledges.
          not_for: Anything about pets.
    """)
    write(org / "agents/fundraising/prompt.md", "You handle donors.\n")
    write(org / "skills/greet.disabled", "")
    write(org / "plays/weekly/play.yaml", "name: weekly\ndescription: Weekly report.\nsteps: []\n")
    project.mkdir()
    shutil.copy(FIXTURES / "petstore.yaml", base / "petstore.yaml")
    write(base / "tools/tools.yaml", """
        sources:
          - kind: openapi
            name: pets
            spec: ../petstore.yaml
            base_url: https://petstore.test
        allow: [pet_list, pet_get, owner_list, pet_delete]
    """)
    return [("baseline", base), ("pack:a", pack), ("org", org), ("project", project)]


def test_merge_rules_and_sources(stack):
    resolved = resolve_setup(stack)
    assert resolved.setup["models"] == {"main": "small"}
    assert resolved.setup["thresholds"] == {"confidence": 0.8, "other": 1}
    chat = resolved.agents["chat"]
    assert chat["config"]["tools"] == ["pet_list", "owner_list"]  # union
    assert chat["config"]["max_steps"] == 6  # deeper scalar wins
    assert chat["prompt"] == "You answer questions.\n\nBe brief.\n"
    assert chat["prompt_sources"] == ["baseline", "org"]
    assert resolved.skills["charts"]["source"] == "pack:a"
    assert resolved.skills["charts"]["description"] == "Build a chart, pack version."
    assert "greet" not in resolved.skills  # disabled marker in org
    assert resolved.context["house"]["source"] == "pack:a"
    assert resolved.data["teams"]["source"] == "baseline"
    assert resolved.read_data("teams").startswith(b"id,name")
    assert resolved.plays["weekly"]["source"] == "org"
    assert resolved.sources["agents/chat/prompt.append.md"] == "org"


def test_replace_and_disable(stack):
    org = stack[2][1]
    write(org / "agents/chat/agent.yaml", "tools: [owner_list]\nreplace: [tools]\n")
    write(org / "setup.yaml", "agent_types: {canvas: false}\n")
    resolved = resolve_setup(stack)
    assert resolved.agents["chat"]["config"]["tools"] == ["owner_list"]
    assert "canvas" not in resolved.agent_types()


def test_types_mapping_context_and_routes(stack):
    resolved = resolve_setup(stack)
    assert resolved.skills_for("canvas") == ["charts"]
    assert resolved.skills_for("chat") == []
    assert set(resolved.context_for("canvas")) == {"house", "canvas-only"}
    assert set(resolved.context_for("chat")) == {"house"}
    assert resolved.route_options() == [{
        "type": "fundraising", "description": "Questions about donors and pledges.",
        "not_for": "Anything about pets.", "source": "org"}]
    prompt, warnings = resolved.system_prompt("canvas")
    # the pack's charts skill replaced the baseline one, prompt included
    assert "You edit canvases." in prompt and "Pack charts: use lines." in prompt and "Canvas notes." in prompt
    assert "use bars" not in prompt
    assert warnings == []


def test_budget_cut_at_whole_file(stack):
    base = stack[0][1]
    write(base / "setup.yaml", "budgets: {context_chars: 40}\n")
    write(base / "context/zzz.md", "x" * 30 + "\n")
    prompt, warnings = resolve_setup(stack).system_prompt("chat")
    assert "Pack house rules." in prompt and "xxxx" not in prompt
    assert warnings and "context cut at zzz" in warnings[0]


def test_tool_set_union_policy_and_delete(stack):
    tools = resolve_tools(stack, transport=recorder())
    # DELETE is excluded by the source even though allow names it: the
    # openapi source did not name it explicitly
    assert set(tools.names()) == {"pet_list", "pet_get", "owner_list"}
    assert tools.approvals()["pet_list"] == "read" and tools.approvals()["listPets"] == "read"
    capped = resolve_tools(stack, global_allow=["pet_list"], transport=recorder())
    assert capped.names() == ["pet_list"]


def test_global_allow_cannot_be_widened(stack):
    project = stack[3][1]
    write(project / "tools/tools.yaml", "allow: ['*']\n")
    tools = resolve_tools(stack, global_allow=["pet_get", "owner_*"], transport=recorder())
    assert set(tools.names()) == {"pet_get", "owner_list"}
    # writing an allow entry the global list does not hold is refused
    with pytest.raises(SetupError):
        put_item(project, "tools", "tools.yaml", "allow: [pet_create]\n",
                 policy=ToolPolicy.permissive(["pet_get"]))


def test_project_put_adds_callable_tool_on_next_resolve(stack):
    project = stack[3][1]
    calls: list[httpx.Request] = []
    transport = recorder(calls)
    before = resolve_tools(stack, transport=transport)
    assert "pet_adopt" not in before.names()
    put_item(project, "tools", "tools.yaml", textwrap.dedent("""
        allow: [pet_adopt]
    """))
    after = resolve_tools(stack, transport=transport)
    assert "pet_adopt" in after.names() and after.get("pet_adopt").layer == "baseline"
    out = after.registry().invoke("pet_adopt", {"petId": 2, "owner_id": 5})
    assert out["ok"] and calls[-1].url.path == "/pets/2/adopt"
    diff = diff_snapshots(before.snapshot(), after.snapshot())
    assert diff["added"] == ["pet_adopt"] and diff["removed"] == []


def test_two_products_same_path(tmp_path):
    """A second product with a $ref-heavy spec uses the same code path."""
    layer = tmp_path / "clinic"
    shutil.copy(FIXTURES / "clinic.json", tmp_path / "clinic.json")
    write(layer / "tools/tools.yaml", """
        sources:
          - kind: openapi
            name: clinic
            spec: ../../clinic.json
            strip_prefixes: [admin]
            dry_run_header: X-Dry-Run
        allow: ["clinic_*", "Appointments_Replace"]
    """)
    write(layer / "agents/scheduler/agent.yaml", "tools: [clinic_appointment_create]\n")
    calls: list[httpx.Request] = []
    tools = resolve_tools([("clinic", layer)], transport=recorder(calls, 409), dry_run=True)
    assert set(tools.names()) == {"clinic_appointment_list", "clinic_appointment_create", "appointment_replace"}
    out = tools.registry().invoke("Appointments_Book", {"clinicId": "north", "patient": "p1", "slot": "2026-01-01T10:00:00Z"})
    assert out["result"]["status"] == "conflict"
    assert calls[-1].headers["X-Dry-Run"] == "true"


def test_hot_reload_and_cache(stack):
    first = resolve_setup(stack)
    assert resolve_setup(stack) is first  # unchanged tree hits the cache
    write(stack[3][1] / "context/news.md", "Fresh news.\n")
    second = resolve_setup(stack)
    assert second is not first and "news" in second.context
    t1 = resolve_tools(second, transport=recorder())
    assert resolve_tools(second, transport=t1 and recorder()) is not None
    # hidden state files never change the hash
    (stack[3][1] / "tools" / ".fabri-state").mkdir(parents=True)
    (stack[3][1] / "tools" / ".fabri-state" / "sync.json").write_text("{}")
    assert resolve_setup(stack).content_hash == second.content_hash


def test_put_delete_and_validation(stack):
    project = stack[3][1]
    result = put_item(project, "skills", "notes", {"skill.yaml": "name: notes\ndescription: Notes.\ntypes: [chat]\n",
                                                   "prompt.md": "Keep notes short.\n"})
    assert result.path == project / "skills" / "notes"
    assert resolve_setup(stack).skills["notes"]["source"] == "project"
    put_item(project, "skills", "notes/prompt.md", "Keep notes very short.\n")
    assert resolve_setup(stack).skills["notes"]["prompt"] == "Keep notes very short.\n"
    with pytest.raises(SetupError):
        put_item(project, "skills", "other", {"skill.yaml": "name: wrong\ndescription: x\n"})
    with pytest.raises(SetupError):
        put_item(project, "skills", "../escape", {"skill.yaml": "name: escape\ndescription: x\n"})
    with pytest.raises(SetupError):
        put_item(project, "agents", "chat", {"agent.yaml": "route: {description: only}\n"})
    with pytest.raises(SetupError):
        put_item(project, "skills", "big", {"skill.yaml": "name: big\ndescription: x\n", "prompt.md": "y" * 5000})
    warn = put_item(project, "context", "tone", "We leverage robust synergy.\n")
    assert warn.warnings and "writing tells" in warn.warnings[0]
    assert delete_item(project, "skills", "notes") is True
    assert "notes" not in resolve_setup(stack).skills
    assert delete_item(project, "skills", "notes") is False
    set_disabled(project, "context", "house")
    assert "house" not in resolve_setup(stack).context
    set_disabled(project, "context", "house", False)
    assert "house" in resolve_setup(stack).context
    with pytest.raises(SetupError):
        put_item(project, "data", "rows.json", "{not json")


def test_per_layer_budget(stack):
    project = stack[3][1]
    write(project / "setup.yaml", "budgets: {skill_prompt_chars: 10}\n")
    with pytest.raises(SetupError):
        put_item(project, "skills", "tiny", {"skill.yaml": "name: tiny\ndescription: x\n", "prompt.md": "y" * 20})


def test_host_validator(stack):
    def no_empty_steps(name, files):
        if "steps: []" in files["play.yaml"]:
            raise ValueError("a play needs steps")

    setups = Setups(stack, validators={"plays": no_empty_steps})
    with pytest.raises(SetupError, match="a play needs steps"):
        setups.put("project", "plays", "empty", "name: empty\ndescription: x\nsteps: []\n")


def test_import_skill_md_idempotent(stack, tmp_path):
    src = tmp_path / "incoming"
    write(src / "chat_canvas/SKILL.md", "---\nname: Chat canvas\ndescription: Draw on the canvas.\ntags: [play]\n---\nDraw it.\n")
    write(src / "follow_up/SKILL.md", "---\nname: Follow up\ndescription: Follow up later.\n---\nAsk again.\n")
    write(src / "broken/SKILL.md", "---\nname: Broken\n---\nNo description.\n")
    project = stack[3][1]
    dry = import_skills(src, project, fmt="skill-md", dry_run=True)
    assert dry.created == ["chat-canvas", "follow-up"] and not (project / "skills").exists()
    first = import_skills(src, project, fmt="skill-md")
    assert first.created == ["chat-canvas", "follow-up"]
    assert [i["name"] for i in first.invalid] == ["broken"]
    meta = yaml.safe_load((project / "skills/chat-canvas/skill.yaml").read_text())
    assert meta["title"] == "Chat canvas" and meta["tags"] == ["play"] and meta["types"] == []
    second = import_skills(src, project, fmt="skill-md")
    assert second.created == [] and second.updated == [] and second.skipped == ["chat-canvas", "follow-up"]
    write(src / "follow_up/SKILL.md", "---\nname: Follow up\ndescription: Follow up later.\n---\nAsk twice.\n")
    third = import_skills(src, project, fmt="skill-md")
    assert third.updated == ["follow-up"] and third.skipped == ["chat-canvas"]


def test_import_fabri_format(stack, tmp_path):
    src = tmp_path / "fabri-skills" / "report"
    write(src / "skill.yaml", "name: report\ndescription: Write a report.\nversion: '2'\n")
    write(src / "prompts/a.md", "Part A.\n")
    write(src / "prompts/b.md", "Part B.\n")
    write(src / "config.yaml", "tools: {enabled: [pet_list]}\n")
    write(src / "tools/run.py", "print(1)\n")
    report = import_skills(src.parent, stack[3][1], fmt="fabri")
    assert report.created == ["report"] and any("not imported" in w for w in report.warnings)
    skill = resolve_setup(stack).skills["report"]
    assert skill["tools"] == ["pet_list"] and skill["prompt"] == "Part A.\n\nPart B.\n"


def test_lint(stack):
    project = stack[3][1]
    write(project / "skills/odd/skill.yaml", "name: odd\ndescription: Odd.\ncolour: red\ntools: [ghost_tool]\norigin: skill-md\n")
    findings = lint_setup(stack, tool_names=["pet_list", "pet_get", "owner_list"])
    messages = [f.message for f in findings]
    assert any("unknown keys ['colour']" in m for m in messages)
    assert any("ghost_tool" in m and f.level == "error" for m, f in zip(messages, findings))
    assert any("loads for every agent type" in m for m in messages)
    assert has_errors(findings)


def test_build_agent_config(stack):
    tools = resolve_tools(stack, transport=recorder())
    resolved = resolve_setup(stack)
    cfg, registry = build_agent_config(resolved, "canvas", {"provider": "openai", "model": "gpt-5-mini"},
                                       tool_set=tools)
    assert cfg["agent"]["name"] == "canvas"
    assert "Pack charts: use lines." in cfg["agent"]["system_prompt_prefix"]
    assert cfg["llm"]["roles"]["main"]["model"] == "gpt-5-mini"
    # canvas asks for pet_get; its skill charts adds pet_list
    assert set(registry.tools) == {"pet_get", "pet_list", "find_tools", "describe_tool"}
    assert cfg["setup"]["approvals"]["pet_get"] == "read"
    assert "find_tools" in cfg["tools"]["retrieval"]["always_include"]
    with pytest.raises(SetupError):
        build_agent_config(resolved, "nope", {}, tool_set=tools)


def test_setups_facade(stack):
    setups = Setups(stack, policy=ToolPolicy.permissive(["pet_*", "owner_list"]), transport=recorder())
    assert set(setups.tools().names()) == {"pet_list", "pet_get"} | {"owner_list"}
    setups.put("project", "context", "extra", "More.\n")
    assert "extra" in setups.resolve().context
    with pytest.raises(SetupError):
        setups.put("nowhere", "context", "x", "y")
    assert json.dumps(setups.resolve().to_dict("canvas"))
