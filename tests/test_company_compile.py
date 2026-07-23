from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.company import CompanyError, compile_company, load_company
from fabri.config import load_config

_RESPONSE_KEYS = {"response_schema", "response_retries", "error_strategy"}


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
    assert root["memory"]["collection"] == "acme_eng_company"
    assert root["memory"]["record_postmortems"] is True
    assert root["memory"]["action_scope"] == {
        "company": "acme_eng",
        "agency": "company",
        "role": "ceo",
    }
    assert "<AGENT_MEMORY>" in root["agent"]["system_prompt"]

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
    assert bug_config["memory"]["action_scope"] == {
        "company": "acme_eng",
        "agency": "bugs",
        "role": "bug-manager",
    }
    assert bug_config["memory"]["collection"] != writer_config["memory"]["collection"]
    for specialist in (
        root_config.parent / "agencies" / "bugs" / "specialist.yaml",
        root_config.parent / "agencies" / "writer" / "specialist.yaml",
    ):
        assert load_config(str(specialist))["llm"]["provider"] == "openai"


def test_compile_company_anchors_all_sqlite_memory_outside_ephemeral_output(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "company_3level" / "company.toml"
    durable_root = tmp_path / "durable"

    first_root = compile_company(
        fixture, tmp_path / "compiled-first", run_from=durable_root
    )
    second_root = compile_company(
        fixture, tmp_path / "compiled-second", run_from=durable_root
    )

    expected_paths = {
        Path("ceo.yaml"): durable_root / ".fabri" / "acme_eng.db",
        Path("vp_eng.yaml"): durable_root / ".fabri" / "acme_eng.db",
        Path("agencies/bugs/agent.openai.yaml"): (
            durable_root / ".fabri" / "acme_eng_bugs.db"
        ),
        Path("agencies/bugs/specialist.yaml"): (
            durable_root / ".fabri" / "acme_eng_bugs.db"
        ),
        Path("agencies/writer/agent.openai.yaml"): (
            durable_root / ".fabri" / "acme_eng_writer.db"
        ),
        Path("agencies/writer/specialist.yaml"): (
            durable_root / ".fabri" / "acme_eng_writer.db"
        ),
    }
    for relative_path, expected_path in expected_paths.items():
        first_config = yaml.safe_load((first_root.parent / relative_path).read_text())
        second_config = yaml.safe_load((second_root.parent / relative_path).read_text())
        first_sqlite_path = Path(first_config["memory"]["sqlite_path"])
        second_sqlite_path = Path(second_config["memory"]["sqlite_path"])

        assert first_sqlite_path == expected_path.resolve()
        assert first_sqlite_path.name == expected_path.name
        assert not first_sqlite_path.is_relative_to(first_root.parent)
        assert second_sqlite_path == first_sqlite_path


def test_compile_company_defaults_memory_root_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = (
        Path(__file__).parent / "fixtures" / "company_3level" / "company.toml"
    ).resolve()
    run_from = tmp_path / "run-from"
    run_from.mkdir()
    monkeypatch.chdir(run_from)

    root_config = compile_company(fixture, tmp_path / "compiled")
    root = yaml.safe_load(root_config.read_text())
    specialist = yaml.safe_load(
        (
            root_config.parent / "agencies" / "bugs" / "specialist.yaml"
        ).read_text()
    )

    assert Path(root["memory"]["sqlite_path"]) == (
        run_from / ".fabri" / "acme_eng.db"
    )
    assert Path(specialist["memory"]["sqlite_path"]) == (
        run_from / ".fabri" / "acme_eng_bugs.db"
    )


def test_compile_company_leaves_qdrant_memory_without_sqlite_path(
    tmp_path: Path,
) -> None:
    agency_dir = tmp_path / "qdrant-agency"
    agency_dir.mkdir()
    (agency_dir / "agency.toml").write_text(
        "[agency]\nname = 'qdrant-agency'\nentry = 'agent.yaml'\n"
    )
    (agency_dir / "agent.yaml").write_text(
        "agent:\n"
        "  name: qdrant-role\n"
        "memory:\n"
        "  backend: qdrant\n"
        "  collection: durable_vectors\n"
        "  qdrant_url: http://localhost:6333\n"
    )
    company = _write_company(
        tmp_path,
        "[[node]]\n"
        "id = 'root'\n"
        "report_to = ''\n\n"
        "[[node]]\n"
        "id = 'crew'\n"
        "report_to = 'root'\n"
        "agency = 'qdrant-agency'\n",
    )

    root_config = compile_company(
        company, tmp_path / "compiled", run_from=tmp_path / "durable"
    )
    role = yaml.safe_load(
        (root_config.parent / "agencies" / "crew" / "agent.yaml").read_text()
    )

    assert role["memory"]["backend"] == "qdrant"
    assert role["memory"]["qdrant_url"] == "http://localhost:6333"
    assert "sqlite_path" not in role["memory"]


def _compile_manager_tree(
    tmp_path: Path, company_extra: str = "", child_extra: str = ""
) -> Path:
    """Compile a minimal root->child manager tree (no leaf agencies needed) so
    the emitted root->child agent entry can be inspected for its timeout."""
    toml = (
        "[company]\nname = 'test-company'\nmemory_namespace = 'test_company'\n"
        + company_extra
        + "\n[[node]]\nid = 'root'\nreport_to = ''\nprompt = 'root'\n\n"
        + "[[node]]\nid = 'child'\nreport_to = 'root'\nprompt = 'child'\n"
        + child_extra
    )
    path = tmp_path / "company.toml"
    path.write_text(toml)
    return compile_company(path, tmp_path / "out")


def _root_child_timeout(root_config: Path) -> float:
    root = yaml.safe_load(root_config.read_text())
    return root["tools"]["agents"][0]["timeout_s"]


def test_call_timeout_defaults_to_900(tmp_path: Path) -> None:
    root_config = _compile_manager_tree(tmp_path)
    assert _root_child_timeout(root_config) == 900.0


def test_company_call_timeout_s_applies(tmp_path: Path) -> None:
    root_config = _compile_manager_tree(tmp_path, company_extra="call_timeout_s = 300\n")
    assert _root_child_timeout(root_config) == 300


def test_node_timeout_s_overrides_company_for_that_child(tmp_path: Path) -> None:
    root_config = _compile_manager_tree(
        tmp_path, company_extra="call_timeout_s = 300\n", child_extra="timeout_s = 45\n"
    )
    assert _root_child_timeout(root_config) == 45


@pytest.mark.parametrize("value", ["'1200'", "true", "0", "-5", "nan", "inf"])
def test_load_company_rejects_bad_call_timeout(tmp_path: Path, value: str) -> None:
    path = tmp_path / "company.toml"
    path.write_text(
        "[company]\nname = 'c'\nmemory_namespace = 'c'\n"
        f"call_timeout_s = {value}\n\n"
        "[[node]]\nid = 'root'\nreport_to = ''\nprompt = 'r'\n"
    )
    with pytest.raises(CompanyError, match="call_timeout_s"):
        load_company(path)


@pytest.mark.parametrize("value", ["'1200'", "true", "-1", "nan"])
def test_load_company_rejects_bad_node_timeout(tmp_path: Path, value: str) -> None:
    path = tmp_path / "company.toml"
    path.write_text(
        "[company]\nname = 'c'\nmemory_namespace = 'c'\n\n"
        "[[node]]\nid = 'root'\nreport_to = ''\nprompt = 'r'\n\n"
        f"[[node]]\nid = 'child'\nreport_to = 'root'\nprompt = 'c'\ntimeout_s = {value}\n"
    )
    with pytest.raises(CompanyError, match="timeout_s"):
        load_company(path)


def test_compile_company_emits_structured_config_only_for_root(tmp_path: Path) -> None:
    path = tmp_path / "company.toml"
    path.write_text(
        "[company]\nname = 'c'\nmemory_namespace = 'c'\n\n"
        "[[node]]\nid = 'root'\nreport_to = ''\nprompt = 'root'\n"
        "response_schema = { type = 'object', required = ['answer'], "
        "properties = { answer = { type = 'string' } } }\n"
        "response_retries = 2\nerror_strategy = 'warn'\n\n"
        "[[node]]\nid = 'child'\nreport_to = 'root'\nprompt = 'child'\n"
    )

    root_path = compile_company(path, tmp_path / "out")
    root = yaml.safe_load(root_path.read_text())
    child = yaml.safe_load((root_path.parent / "child.yaml").read_text())

    assert root["agent"]["response_schema"] == {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    assert root["agent"]["response_retries"] == 2
    assert root["agent"]["error_strategy"] == "warn"
    assert "outside the JSON" in root["agent"]["system_prompt"]
    assert not _RESPONSE_KEYS.intersection(child["agent"])


def test_load_company_rejects_response_schema_on_non_root(tmp_path: Path) -> None:
    path = tmp_path / "company.toml"
    path.write_text(
        "[company]\nname = 'c'\nmemory_namespace = 'c'\n\n"
        "[[node]]\nid = 'root'\nreport_to = ''\nprompt = 'root'\n\n"
        "[[node]]\nid = 'child'\nreport_to = 'root'\nprompt = 'child'\n"
        "response_schema = { type = 'object' }\n"
    )

    with pytest.raises(CompanyError, match="response_schema.*root node"):
        compile_company(path, tmp_path / "out")


def test_load_company_rejects_unsupported_response_schema_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "company.toml"
    path.write_text(
        "[company]\nname = 'c'\nmemory_namespace = 'c'\n\n"
        "[[node]]\nid = 'root'\nreport_to = ''\nprompt = 'root'\n"
        "response_schema = { type = 'object', additionalProperties = false }\n"
    )

    with pytest.raises(CompanyError, match="additionalProperties"):
        compile_company(path, tmp_path / "out")
