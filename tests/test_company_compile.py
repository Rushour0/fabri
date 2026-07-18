from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.company import CompanyError, compile_company, load_company
from fabri.config import load_config


def _write_company(tmp_path: Path, nodes: str) -> Path:
    path = tmp_path / "company.toml"
    path.write_text(
        "[company]\nname = 'test-company'\nmemory_namespace = 'test_company'\n\n"
        + nodes
    )
    return path


@pytest.mark.parametrize(
    "nodes, message",
    [
        (
            "[[node]]\nid = 'root'\nreport_to = ''\n\n"
            "[[node]]\nid = 'a'\nreport_to = 'b'\n\n"
            "[[node]]\nid = 'b'\nreport_to = 'a'\n",
            "cycle",
        ),
        (
            "[[node]]\nid = 'one'\nreport_to = ''\n\n"
            "[[node]]\nid = 'two'\nreport_to = ''\n",
            "exactly one root",
        ),
        (
            "[[node]]\nid = 'root'\nreport_to = ''\n\n"
            "[[node]]\nid = 'child'\nreport_to = 'missing'\n",
            "unknown node",
        ),
        (
            "[[node]]\nid = 'root'\nreport_to = ''\n\n"
            "[[node]]\nid = 'child'\nreport_to = 'root'\n\n"
            "[[node]]\nid = 'child'\nreport_to = 'root'\n",
            "multiple parents",
        ),
    ],
)
def test_load_company_rejects_invalid_trees(
    tmp_path: Path, nodes: str, message: str
) -> None:
    with pytest.raises(CompanyError, match=message):
        load_company(_write_company(tmp_path, nodes))


def test_compile_company_builds_valid_three_level_tree(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "company_3level" / "company.toml"
    root_config = compile_company(fixture, tmp_path)

    assert root_config.exists()
    root = yaml.safe_load(root_config.read_text())
    assert [agent["name"] for agent in root["tools"]["agents"]] == ["vp_eng"]

    vp_path = root_config.parent / "vp_eng.yaml"
    vp = yaml.safe_load(vp_path.read_text())
    assert [agent["name"] for agent in vp["tools"]["agents"]] == ["bugs", "writer"]

    assert load_config(str(root_config))["agent"]["name"] == "ceo"
    bug_entry = root_config.parent / "agencies" / "bugs" / "agent.openai.yaml"
    writer_entry = root_config.parent / "agencies" / "writer" / "agent.openai.yaml"
    bug_config = load_config(str(bug_entry))
    writer_config = load_config(str(writer_entry))
    assert bug_config["memory"]["collection"] == "acme_eng_bugs_manager"
    assert writer_config["memory"]["collection"] == "acme_eng_writer_manager"
    assert bug_config["memory"]["collection"] != writer_config["memory"]["collection"]
