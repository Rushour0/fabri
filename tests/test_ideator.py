"""B1 -- ideator: a scripted backend returning a sample spec must yield a
scaffold dir whose agent.yaml `load_config` accepts and whose tool stubs exist.
No network: every LLM call is a ScriptedLLMBackend."""
from __future__ import annotations

import json

import pytest

from fabri.builder import (
    IDEATION_SPEC_SCHEMA,
    IdeatorError,
    ideate,
    propose_spec,
    scaffold_from_spec,
    spec_to_config,
)
from fabri.config import load_config
from fabri.core import structured
from fabri.core.llm import LLMResponse, ScriptedLLMBackend


_SAMPLE_SPEC = {
    "agent_name": "Note Taker",
    "roles": [
        {"role": "main", "model": "claude-sonnet-4-6", "max_tokens": 2048},
        {"role": "decompose", "model": "claude-haiku-4-5", "max_tokens": 512},
    ],
    "budgets": {"max_steps": 15, "max_cost_usd": 0.5},
    "domains": [
        {"name": "Capture", "prompt_summary": "Record incoming items."},
        {"name": "Summarize", "prompt_summary": "Condense recorded items."},
    ],
    "tools_to_build": [
        {"name": "save item", "description": "Persist a single item."},
        {"name": "list items", "description": "Return all stored items."},
    ],
    "system_prompt_prefix": "You are a meticulous note taker.",
}


def _backend(spec: dict) -> ScriptedLLMBackend:
    return ScriptedLLMBackend([LLMResponse(final_text=json.dumps(spec))])


# ---------------------------------------------------------------------------
# the sample spec itself must satisfy the schema the model is held to
# ---------------------------------------------------------------------------


def test_sample_spec_matches_ideation_schema():
    assert structured.validate(_SAMPLE_SPEC, IDEATION_SPEC_SCHEMA) == []


def test_propose_spec_parses_scripted_reply():
    spec = propose_spec("a tool to take notes", _backend(_SAMPLE_SPEC))
    assert spec["agent_name"] == "Note Taker"


def test_propose_spec_requires_a_backend():
    with pytest.raises(IdeatorError, match="no LLM backend"):
        propose_spec("an idea", None)


def test_propose_spec_rejects_unusable_reply():
    bad = ScriptedLLMBackend([LLMResponse(final_text="not json at all")])
    with pytest.raises(IdeatorError, match="usable spec"):
        propose_spec("an idea", bad)


def test_propose_spec_rejects_schema_mismatch():
    # missing the required `roles`/`tools_to_build` etc.
    backend = ScriptedLLMBackend([LLMResponse(final_text=json.dumps({"agent_name": "x"}))])
    with pytest.raises(IdeatorError, match="usable spec"):
        propose_spec("an idea", backend)


# ---------------------------------------------------------------------------
# spec_to_config -> a config load_config accepts
# ---------------------------------------------------------------------------


def test_spec_to_config_is_load_config_compatible(tmp_path):
    import yaml

    config = spec_to_config(_SAMPLE_SPEC)
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(config))
    loaded = load_config(str(path))

    assert loaded["agent"]["name"] == "Note Taker"
    assert loaded["agent"]["max_steps"] == 15
    assert loaded["agent"]["max_cost_usd"] == 0.5
    assert loaded["agent"]["system_prompt_prefix"] == "You are a meticulous note taker."
    # main role -> top-level llm.*; decompose -> a per-role override block
    assert loaded["llm"]["model"] == "claude-sonnet-4-6"
    assert loaded["llm"]["roles"]["decompose"]["model"] == "claude-haiku-4-5"
    # generated tool identifiers are enabled
    assert "save_item" in loaded["tools"]["enabled"]
    assert "list_items" in loaded["tools"]["enabled"]


def test_spec_to_config_drops_invalid_response_schema():
    spec = {**_SAMPLE_SPEC, "response_schema": {"type": "not-a-real-type"}}
    config = spec_to_config(spec)
    assert "response_schema" not in config["agent"]


def test_spec_to_config_keeps_valid_response_schema():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    spec = {**_SAMPLE_SPEC, "response_schema": schema}
    config = spec_to_config(spec)
    assert config["agent"]["response_schema"] == schema


# ---------------------------------------------------------------------------
# end-to-end: ideate writes a dir load_config accepts with the expected tools
# ---------------------------------------------------------------------------


def test_ideate_writes_reviewable_scaffold(tmp_path):
    out = tmp_path / "note-taker-agent"
    summary = ideate("a tool to take notes", _backend(_SAMPLE_SPEC), out_dir=out)

    # agent.yaml loads and carries the generated tools
    yaml_path = out / "agent.yaml"
    assert yaml_path.is_file()
    loaded = load_config(str(yaml_path))
    enabled = loaded["tools"]["enabled"]
    assert "save_item" in enabled
    assert "list_items" in enabled

    # tool stubs exist: a manifest + an executable per generated tool
    tools_dir = out / "tools" / "agent_tools"
    for ident in ("save_item", "list_items"):
        assert (tools_dir / f"{ident}.json").is_file()
        assert (tools_dir / f"{ident}.py").is_file()
        manifest = json.loads((tools_dir / f"{ident}.json").read_text())
        assert manifest["name"] == ident

    # a system prompt + one prompt per domain, via the prompt-kit
    assert (out / "prompts" / "system.md").is_file()
    assert (out / "prompts" / "capture.md").is_file()
    assert (out / "prompts" / "summarize.md").is_file()

    assert summary["next_command"].startswith("fabri --config")
    assert summary["spec"]["agent_name"] == "Note Taker"


def test_ideate_default_out_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = ideate("a tool to take notes", _backend(_SAMPLE_SPEC))
    assert summary["root"] == "note-taker-agent"
    assert (tmp_path / "note-taker-agent" / "agent.yaml").is_file()


def test_scaffold_refuses_nonempty_dir(tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    (out / "keepme.txt").write_text("do not touch")
    with pytest.raises(IdeatorError, match="already exists"):
        scaffold_from_spec(_SAMPLE_SPEC, out)
    # the existing file was left untouched (review-only, never in place)
    assert (out / "keepme.txt").read_text() == "do not touch"


def test_scaffold_force_overwrites(tmp_path):
    out = tmp_path / "agentdir"
    out.mkdir()
    (out / "stray.txt").write_text("x")
    summary = scaffold_from_spec(_SAMPLE_SPEC, out, force=True)
    assert (out / "agent.yaml").is_file()
    assert summary["root"] == str(out)
