"""Skill structure: metadata fields, on-demand loading with load_skill,
references/ files, and revisions."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.setups import (
    SetupError,
    build_agent_config,
    delete_item,
    import_skills,
    item_history,
    item_revision,
    put_item,
    resolve_setup,
)

BIG = "Step one. " * 1600  # 16,000 chars


def skill(layer: Path, name: str, *, body: str = "Do the thing.", **meta) -> None:
    manifest = {"name": name, "description": f"{name} description", **meta}
    put_item(layer, "skills", name, {"skill.yaml": yaml.safe_dump(manifest), "prompt.md": body})


@pytest.fixture
def layer(tmp_path):
    root = tmp_path / "base"
    (root / "agents" / "chat").mkdir(parents=True)
    (root / "agents" / "chat" / "agent.yaml").write_text("title: Chat\n")
    (root / "agents" / "chat" / "prompt.md").write_text("You help.\n")
    return root


def test_always_skill_stays_in_prompt(layer):
    skill(layer, "small", body="SMALL BODY")
    prompt, warnings = resolve_setup([("base", layer)]).system_prompt("chat")
    assert "SMALL BODY" in prompt and "on demand" not in prompt and warnings == []


def test_on_demand_lists_description_only(layer):
    skill(layer, "big", body=BIG, load="on_demand", when_to_use="the user asks for a chart")
    resolved = resolve_setup([("base", layer)])
    prompt, warnings = resolved.system_prompt("chat")
    assert "Step one." not in prompt and warnings == []
    assert "- `big`: big description Use when: the user asks for a chart" in prompt
    assert len(prompt) < 600  # 16,000 chars of skill stay out of the prompt


def test_big_skill_without_on_demand_is_refused_with_hint(layer):
    with pytest.raises(SetupError, match="load: on_demand"):
        skill(layer, "big", body=BIG)


def test_setup_default_and_always_tag(layer):
    (layer / "setup.yaml").write_text("skill_load: on_demand\n")
    skill(layer, "lazy", body="LAZY")
    skill(layer, "gate", body="GATE", tags=["always"])
    prompt, _ = resolve_setup([("base", layer)]).system_prompt("chat")
    assert "GATE" in prompt and "LAZY" not in prompt and "`lazy`" in prompt


def test_invalid_load_and_when_to_use(layer):
    with pytest.raises(SetupError, match="load must be"):
        skill(layer, "x", load="sometimes")
    with pytest.raises(SetupError, match="when_to_use"):
        skill(layer, "y", when_to_use=3)


def test_load_skill_tool_returns_text_and_exposes_tools(layer):
    skill(layer, "big", body=BIG, load="on_demand", tools=["note_add"])
    resolved = resolve_setup([("base", layer)])
    config, registry = build_agent_config(resolved, "chat", {"provider": "anthropic", "model": "m"})
    assert "load_skill" in registry.tools and "load_skill" in config["tools"]["retrieval"]["always_include"]
    assert config["setup"]["on_demand_skills"] == ["big"]
    out = registry.invoke("load_skill", {"name": "big"})
    assert out["ok"] and out["result"]["text"].startswith("Step one.") and out["result"]["revision"] == 1
    assert registry.invoke("load_skill", {"name": "nope"})["ok"] is False
    assert "load_skill" in config["agent"]["system_prompt_prefix"]


def test_no_load_skill_without_on_demand_skills(layer):
    skill(layer, "small")
    _, registry = build_agent_config(resolve_setup([("base", layer)]), "chat", {"provider": "anthropic", "model": "m"})
    assert "load_skill" not in registry.tools


def test_references_load_on_demand_and_stay_inside_the_skill(layer):
    manifest = yaml.safe_dump({"name": "doc", "description": "d", "load": "on_demand"})
    put_item(layer, "skills", "doc", {"skill.yaml": manifest, "prompt.md": "main",
                                      "references/api.md": "API TEXT"})
    resolved = resolve_setup([("base", layer)])
    assert resolved.skills["doc"]["references"] == ["references/api.md"]
    assert "(1 reference file)" in resolved.system_prompt("chat")[0]
    assert resolved.load_skill("chat", "doc", "references/api.md")["text"] == "API TEXT"
    with pytest.raises(SetupError):
        resolved.load_skill("chat", "doc", "../skill.yaml")
    with pytest.raises(SetupError, match="unexpected files"):
        put_item(layer, "skills", "doc", {"skill.yaml": manifest, "notes/x.md": "no"})
    put_item(layer, "skills", "doc/references/more.md", "MORE")
    assert (layer / "skills/doc/references/more.md").read_text() == "MORE"


def test_revision_counts_changes_and_ignores_identical_writes(layer):
    skill(layer, "s", body="one")
    skill(layer, "s", body="one")
    assert item_revision(layer, "skills", "s") == 1
    skill(layer, "s", body="two", )
    assert item_revision(layer, "skills", "s") == 2
    put_item(layer, "skills", "s", {"skill.yaml": yaml.safe_dump({"name": "s", "description": "d"}),
                                    "prompt.md": "three"}, actor="ana@example.com")
    delete_item(layer, "skills", "s", actor="bo@example.com")
    hist = item_history(layer, "skills", "s")
    assert [h["revision"] for h in hist] == [1, 2, 3, 4]
    assert hist[2]["actor"] == "ana@example.com" and hist[3]["deleted"] is True


def test_revision_file_does_not_change_the_content_hash(layer):
    skill(layer, "s")
    before = resolve_setup([("base", layer)]).content_hash
    skill(layer, "s", body="Changed.")
    assert (layer / ".fabri-revisions.json").is_file()
    assert resolve_setup([("base", layer)]).skills["s"]["revision"] == 2
    (layer / ".fabri-revisions.json").unlink()
    assert resolve_setup([("base", layer)], use_cache=False).content_hash != before  # prompt changed
    skill(layer, "s", body="Do the thing.")
    assert resolve_setup([("base", layer)], use_cache=False).content_hash == before


def test_import_keeps_when_to_use_references_and_marks_big_skills(tmp_path, layer):
    src = tmp_path / "src" / "chat_canvas"
    (src / "references").mkdir(parents=True)
    (src / "SKILL.md").write_text(f"---\nname: Canvas\ndescription: Draft things.\nwhen_to_use: asked to draft\n---\n{BIG}")
    (src / "references" / "blocks.md").write_text("BLOCKS")
    report = import_skills(tmp_path / "src", layer)
    assert report.created == ["chat-canvas"] and any("on_demand" in w for w in report.warnings)
    meta = yaml.safe_load((layer / "skills/chat-canvas/skill.yaml").read_text())
    assert meta["load"] == "on_demand" and meta["when_to_use"] == "asked to draft"
    assert (layer / "skills/chat-canvas/references/blocks.md").read_text() == "BLOCKS"
    assert import_skills(tmp_path / "src", layer).skipped == ["chat-canvas"]

