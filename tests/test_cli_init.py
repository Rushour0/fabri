"""`fabri init` scaffolds a project that actually resolves -- the generated
agent.yaml + example tool must load through the real registry, so a new user's
first `fabri run` works."""
import os

import yaml

from fabri.config import load_config
from fabri.runtime import build_tools
from fabri.scaffold import scaffold


def test_init_writes_expected_files(tmp_path):
    result = scaffold(str(tmp_path))
    for rel in ("agent.yaml", "tools/agent_tools/hello.py",
                "tools/agent_tools/hello.json", "docker-compose.yml", ".gitignore"):
        assert (tmp_path / rel).exists(), rel
    assert set(result["created"]) and not result["skipped"]
    assert ".fabri/" in (tmp_path / ".gitignore").read_text()


def test_init_does_not_overwrite_without_force(tmp_path):
    (tmp_path / "agent.yaml").write_text("custom: keep me\n")
    result = scaffold(str(tmp_path))
    assert "agent.yaml" in result["skipped"]
    assert (tmp_path / "agent.yaml").read_text() == "custom: keep me\n"
    # ...but --force overwrites
    result = scaffold(str(tmp_path), force=True)
    assert "agent.yaml" in result["created"]


def test_scaffolded_config_resolves_builtin_and_custom_tools(tmp_path):
    scaffold(str(tmp_path))
    config = load_config(str(tmp_path / "agent.yaml"))
    assert yaml.safe_load((tmp_path / "agent.yaml").read_text())  # valid YAML
    cwd = os.getcwd()
    os.chdir(tmp_path)  # manifest_dir + sandbox_root resolve relative to cwd
    try:
        registry = build_tools(config["tools"])
    finally:
        os.chdir(cwd)
    # builtin tools and the scaffolded custom tool both resolve
    assert {"read_file", "write_file", "list_dir", "hello"} <= set(registry.tools)
