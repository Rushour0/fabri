"""B4 -- skills registry: discovering, merging, and installing a skill bundle is
pure file work (no LLM, no network). These tests install the bundled `clock`
example into a temp project and assert the tool manifest + prompt land and the
config gains the skill's keys without losing existing ones."""
from __future__ import annotations

import json

import pytest
import yaml

from fabri.builder import (
    SkillError,
    discover_skills,
    install_skill,
    load_skill,
    merge_skill_config,
    new_skill,
    resolve_skill,
)
from fabri.builder.skills import BUNDLED_SKILLS_DIR
from fabri.config import load_config


# ---------------------------------------------------------------------------
# discovery / load
# ---------------------------------------------------------------------------


def test_bundled_clock_skill_loads():
    skill = load_skill(BUNDLED_SKILLS_DIR / "clock")
    assert skill.name == "clock"
    assert skill.version == "0.1.0"
    assert skill.config_path is not None
    assert skill.tools_dir is not None
    assert skill.prompts_dir is not None


def test_discover_includes_bundled_clock():
    names = {s.name for s in discover_skills()}
    assert "clock" in names


def test_load_skill_rejects_missing_manifest(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SkillError):
        load_skill(tmp_path / "empty")


def test_load_skill_rejects_invalid_manifest(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "skill.yaml").write_text("name: bad\n")  # missing description + version
    with pytest.raises(SkillError):
        load_skill(d)


def test_resolve_skill_unknown_name_lists_available(tmp_path):
    with pytest.raises(SkillError) as exc:
        resolve_skill("does-not-exist", skills_dir=tmp_path / "skills")
    assert "clock" in str(exc.value)


def test_resolve_project_skill_shadows_by_name(tmp_path):
    skills_dir = tmp_path / "skills"
    new_skill("clock", target_dir=skills_dir)
    resolved = resolve_skill("clock", skills_dir=skills_dir)
    assert resolved.path == (skills_dir / "clock")


# ---------------------------------------------------------------------------
# config merge: additive, project-wins, list-union, conflict reporting
# ---------------------------------------------------------------------------


def test_merge_adds_skill_keys_and_keeps_existing():
    project = {"agent": {"name": "mine"}, "llm": {"model": "x"}}
    skill = {"tools": {"enabled": ["now"]}}
    merged, conflicts = merge_skill_config(project, skill)
    assert merged["agent"]["name"] == "mine"
    assert merged["llm"]["model"] == "x"
    assert merged["tools"]["enabled"] == ["now"]
    assert conflicts == []


def test_merge_unions_lists_without_dropping_project_items():
    project = {"tools": {"enabled": ["read_file"], "manifest_dir": ["builtin"]}}
    skill = {"tools": {"enabled": ["now", "read_file"], "manifest_dir": ["tools/agent_tools"]}}
    merged, conflicts = merge_skill_config(project, skill)
    assert merged["tools"]["enabled"] == ["read_file", "now"]
    assert merged["tools"]["manifest_dir"] == ["builtin", "tools/agent_tools"]
    assert conflicts == []


def test_merge_reports_scalar_conflict_and_keeps_project_value():
    project = {"agent": {"max_steps": 10}}
    skill = {"agent": {"max_steps": 99}}
    merged, conflicts = merge_skill_config(project, skill)
    assert merged["agent"]["max_steps"] == 10
    assert len(conflicts) == 1
    assert "agent.max_steps" in conflicts[0]


def test_merge_is_idempotent():
    project = {"tools": {"enabled": ["read_file"]}}
    skill = {"tools": {"enabled": ["now"]}}
    once, _ = merge_skill_config(project, skill)
    twice, conflicts = merge_skill_config(once, skill)
    assert once == twice
    assert conflicts == []


# ---------------------------------------------------------------------------
# install: the acceptance path
# ---------------------------------------------------------------------------


def _existing_agent_yaml(project) -> None:
    """Seed a project agent.yaml with content the install must not clobber."""
    (project / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {"name": "host-agent", "max_steps": 7},
                "tools": {"enabled": ["read_file"]},
            }
        )
    )


def test_install_clock_lands_tool_prompt_and_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _existing_agent_yaml(project)

    summary = install_skill("clock", project)

    # tool manifest + executable landed in tools/agent_tools
    manifest = project / "tools" / "agent_tools" / "now.json"
    executable = project / "tools" / "agent_tools" / "now.py"
    assert manifest.is_file()
    assert executable.is_file()
    assert json.loads(manifest.read_text())["name"] == "now"

    # prompt landed in prompts/
    assert (project / "prompts" / "clock.md").is_file()

    # config gained the skill's keys without losing existing ones
    cfg = yaml.safe_load((project / "agent.yaml").read_text())
    assert cfg["agent"]["name"] == "host-agent"   # preserved
    assert cfg["agent"]["max_steps"] == 7          # preserved
    assert "read_file" in cfg["tools"]["enabled"]  # preserved
    assert "now" in cfg["tools"]["enabled"]        # added
    assert "tools/agent_tools" in cfg["tools"]["manifest_dir"]  # added

    assert summary["conflicts"] == []
    # the merged config is still a valid fabri config
    load_config(str(project / "agent.yaml"))


def test_install_creates_agent_yaml_when_absent(tmp_path):
    project = tmp_path / "fresh"
    install_skill("clock", project)
    cfg = yaml.safe_load((project / "agent.yaml").read_text())
    assert cfg["tools"]["enabled"] == ["now"]
    assert (project / "tools" / "agent_tools" / "now.py").is_file()


def test_install_is_idempotent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _existing_agent_yaml(project)

    install_skill("clock", project)
    first = (project / "agent.yaml").read_text()
    second_summary = install_skill("clock", project)
    second = (project / "agent.yaml").read_text()

    assert first == second                     # config unchanged on re-run
    assert second_summary["tools"] == []       # nothing re-copied
    assert len(second_summary["skipped"]) >= 2  # tool + prompt skipped
    assert second_summary["conflicts"] == []


def test_install_resolves_a_skill_directory_path(tmp_path):
    project = tmp_path / "project"
    summary = install_skill(str(BUNDLED_SKILLS_DIR / "clock"), project)
    assert summary["skill"] == "clock"
    assert (project / "tools" / "agent_tools" / "now.json").is_file()


# ---------------------------------------------------------------------------
# add: scaffold a new skill skeleton, round-trip through load
# ---------------------------------------------------------------------------


def test_new_skill_scaffolds_loadable_skeleton(tmp_path):
    result = new_skill("mycap", target_dir=tmp_path / "skills")
    assert result["created"]
    skill = load_skill(result["root"])
    assert skill.name == "mycap"
    # the skeleton's config snippet must parse as YAML
    assert skill.config_path is not None
    yaml.safe_load(skill.config_path.read_text())


def test_new_skill_rejects_bad_name(tmp_path):
    with pytest.raises(SkillError):
        new_skill("bad name!", target_dir=tmp_path / "skills")
