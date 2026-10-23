"""The jev-router agency template scaffolds, its specialist configs load, and
its tree.yaml validates with a cascade decider."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.agency_scaffold import scaffold_agency
from fabri.agency_templates import TEMPLATES
from fabri.config import load_config
from fabri.decide import CascadeDecider, DecisionTree, JevDecider, LLMDecider


def test_template_registered():
    assert "jev-router" in TEMPLATES


def test_scaffold_and_tree_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    created = scaffold_agency("router-demo", "jev-router", Path("agencies"))
    agency = tmp_path / "agencies" / "router-demo"
    names = {p.relative_to(Path("agencies") / "router-demo").as_posix() for p in created}
    assert names == {"tree.yaml", "chat.openai.yaml", "canvas.openai.yaml",
                     "tasks.openai.yaml", "workspace/.gitignore", "README.md"}

    for p in created:
        text = p.read_text()
        assert "__AGENCY_ROOT__" not in text and "__AGENCY_SLUG__" not in text
        assert "__RUN_FROM__" not in text

    tree = DecisionTree.from_yaml(agency / "tree.yaml")
    assert isinstance(tree.decider, CascadeDecider)
    assert isinstance(tree.decider.primary, JevDecider)
    assert isinstance(tree.decider.fallback, LLMDecider)
    assert tree.start == "intent"
    assert set(tree.nodes) == {
        "intent", "canvas_affects_others", "canvas_hard_to_undo",
        "affects_others", "hard_to_undo", "bulk_change",
    }

    # Every leaf points at a specialist config that exists and loads.
    leaves = [t for node in tree.nodes.values() for t in node.branches.values() if isinstance(t, dict)]
    assert {leaf["agent"] for leaf in leaves} == {"canvas", "chat", "tasks"}
    assert any(leaf.get("requires_approval") is True for leaf in leaves)
    for leaf in leaves:
        config_path = tmp_path / leaf["config"]
        assert config_path.is_file()
        config = load_config(str(config_path))
        assert config["llm"]["model"] == "gpt-5-mini"
        assert config["agent"]["max_steps"] <= 5
        assert (tmp_path / config["tools"]["sandbox_root"]).is_dir()

    collections = {yaml.safe_load((agency / f"{n}.openai.yaml").read_text())["memory"]["collection"]
                   for n in ("chat", "canvas", "tasks")}
    assert len(collections) == 3


def test_every_mutating_path_has_approval_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    scaffold_agency("router-demo", "jev-router", Path("agencies"))
    tree = DecisionTree.from_yaml(tmp_path / "agencies" / "router-demo" / "tree.yaml")

    def walk(node_name: str, yesno_nodes: int = 0) -> None:
        node = tree.nodes[node_name]
        yesno_nodes += node.question.kind == "yesno"
        for target in node.branches.values():
            if isinstance(target, str):
                walk(target, yesno_nodes)
            elif target["agent"] in {"canvas", "tasks"} and not target.get("requires_approval"):
                assert yesno_nodes >= 2

    walk(tree.start)


def test_readme_explains_the_routing_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    scaffold_agency("r", "jev-router", Path("."))
    readme = (tmp_path / "r" / "README.md").read_text()
    assert "fallback" in readme
    assert "requires_approval: true" in readme
    assert "—" not in readme
