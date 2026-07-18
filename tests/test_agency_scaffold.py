from pathlib import Path

import pytest
import yaml

from fabri.agency_scaffold import scaffold_agency


@pytest.mark.parametrize("template", ["bug-crew", "changelog", "blank"])
def test_scaffolded_agency_config_paths_exist(
    template: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    name = f"{template}-demo"

    created = scaffold_agency(name, template, Path("agencies"))
    agency_dir = tmp_path / "agencies" / name
    parent_path = agency_dir / "agent.openai.yaml"

    assert created
    assert parent_path.is_file()
    parent = yaml.safe_load(parent_path.read_text())

    for agent in parent["tools"].get("agents", []):
        assert (tmp_path / agent["config"]).is_file()

    for config_path in agency_dir.glob("*.yaml"):
        config = yaml.safe_load(config_path.read_text())
        assert (tmp_path / config["tools"]["sandbox_root"]).is_dir()

    verify_command = parent["agent"].get("repair", {}).get("verify_command")
    if verify_command:
        script = next(arg for arg in verify_command if str(arg).endswith(".py"))
        assert (tmp_path / script).is_file()

    if template == "bug-crew":
        workspace = agency_dir / "workspace"
        assert (workspace / "store.py").is_file()
        assert (workspace / "test_store.py").is_file()
        assert not (workspace / "__pycache__").exists()
        assert "return subtotal * discount" in (workspace / "store.py").read_text()
        assert "sys.path.insert" in (workspace / "test_store.py").read_text()

    if template == "changelog":
        check_script = agency_dir / "scripts" / "check_release_notes.py"
        assert f"agencies/{name}/source/release_input.json" in check_script.read_text()
