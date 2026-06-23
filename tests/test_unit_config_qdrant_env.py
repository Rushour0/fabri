"""`QDRANT_URL` env override on load_config: lets a containerized host
(orchestrator + spawn_subagent tool + child sub-agents all inherit the env)
point fabri at a reachable qdrant without rewriting each on-disk yaml. Without
it, a child loading the repo yaml's `localhost:6333` dies on connect in Docker.
"""
from fabri import config as cfgmod
from fabri.config import load_config


def test_qdrant_url_env_overrides_yaml(tmp_path, monkeypatch):
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  collection: c\n  qdrant_url: http://localhost:6333\n")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    cfg = load_config(str(cfg_file))
    assert cfg["memory"]["qdrant_url"] == "http://qdrant:6333"
    assert cfg["memory"]["collection"] == "c"  # other fields untouched


def test_no_env_keeps_yaml_value(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  qdrant_url: http://localhost:6333\n")
    cfg = load_config(str(cfg_file))
    assert cfg["memory"]["qdrant_url"] == "http://localhost:6333"


def test_env_applies_to_default_config_without_mutating_it(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    cfg = load_config(None)
    assert cfg["memory"]["qdrant_url"] == "http://qdrant:6333"
    # The shared module-level DEFAULT_CONFIG must NOT be mutated by the override.
    assert cfgmod.DEFAULT_CONFIG["memory"]["qdrant_url"] == "http://localhost:6333"
