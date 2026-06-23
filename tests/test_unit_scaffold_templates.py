"""Unit tests for `fabri init --template <name>` (G18).

Each template must scaffold a runnable starter (valid YAML, tools resolve,
no docker hard-dependency on the sqlite-vec templates). We don't actually
run the agent; we only assert the files land and parse.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.scaffold import SCAFFOLD_TEMPLATES, next_steps, scaffold


@pytest.mark.parametrize("template", sorted(SCAFFOLD_TEMPLATES.keys()))
def test_scaffold_template_writes_agent_yaml_and_gitignore(tmp_path, template):
    """Every template must ship at least agent.yaml + .gitignore."""
    result = scaffold(str(tmp_path), template=template)
    created = set(result["created"])
    assert "agent.yaml" in created
    assert ".gitignore" in created
    assert (tmp_path / "agent.yaml").exists()


@pytest.mark.parametrize("template", sorted(SCAFFOLD_TEMPLATES.keys()))
def test_scaffold_template_agent_yaml_parses_as_yaml(tmp_path, template):
    """A broken template = a broken first run. Every starter must round-trip
    through PyYAML cleanly and contain the load-bearing top-level keys."""
    scaffold(str(tmp_path), template=template)
    cfg = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert set(cfg.keys()) >= {"agent", "llm", "tools", "memory"}


def test_scaffold_unknown_template_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        scaffold(str(tmp_path), template="nope")


def test_scaffold_research_template_uses_sqlite_backend(tmp_path):
    """The non-default templates intentionally use sqlite-vec so the
    quickstart doesn't require docker."""
    scaffold(str(tmp_path), template="research")
    cfg = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert cfg["memory"]["backend"] == "sqlite"


def test_scaffold_default_template_still_uses_qdrant(tmp_path):
    """The original default scaffold (back-compat) keeps Qdrant. Templates
    are opt-in; existing tutorials keep working."""
    scaffold(str(tmp_path))  # default
    cfg = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    # Default config doesn't necessarily mention backend (falls back to qdrant
    # via config.DEFAULT_CONFIG), so accept either omission or explicit qdrant.
    assert cfg["memory"].get("backend", "qdrant") == "qdrant"


def test_scaffold_skips_existing_files_without_force(tmp_path):
    """Re-running scaffold must not clobber an existing agent.yaml unless
    --force is set."""
    (tmp_path / "agent.yaml").write_text("# my own config")
    result = scaffold(str(tmp_path))
    assert "agent.yaml" in result["skipped"]
    assert (tmp_path / "agent.yaml").read_text() == "# my own config"


def test_scaffold_force_overwrites_existing(tmp_path):
    (tmp_path / "agent.yaml").write_text("# my own config")
    result = scaffold(str(tmp_path), force=True)
    assert "agent.yaml" in result["created"]
    assert (tmp_path / "agent.yaml").read_text() != "# my own config"


def test_next_steps_for_non_default_template_does_not_require_docker(tmp_path):
    """Templates using sqlite must communicate "no docker needed" in the
    next-steps hint — otherwise the user thinks they still need it."""
    text = next_steps(str(tmp_path), template="research")
    assert "no docker" in text.lower()
    assert "docker compose" not in text


def test_next_steps_for_default_template_mentions_docker(tmp_path):
    """The default template requires Qdrant via docker; the hint must say so."""
    text = next_steps(str(tmp_path), template="default")
    assert "docker compose" in text
