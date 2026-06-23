"""Thorough coverage for CONFIG, SYSTEM-PROMPT, and SPAWN plumbing.

Complements (does NOT duplicate) tests/test_unit_config_qdrant_env.py,
tests/test_system_prompt_frugality.py, and tests/test_spawn_subagent.py with
distinct edge cases for:

  - fabri.config.load_config / _apply_env_overrides QDRANT_URL handling
  - fabri.core.agent.build_system_prompt policy gating + ordering
  - fabri.tools.examples.spawn_subagent.build_runner_command flag plumbing
"""
import sys
from pathlib import Path

import pytest

from fabri import config as cfgmod
from fabri.config import load_config
from fabri.core.agent import (
    CODE_ACTION_POLICY,
    DELEGATION_POLICY,
    DEFAULT_AGENT_IDENTITY,
    FILE_EDIT_POLICY,
    FRUGALITY_POLICY,
    TOON_RESULT_NOTE,
    build_system_prompt,
)
from fabri.tools.examples.spawn_subagent import (
    build_runner_command,
    sanitize_collection_suffix,
)


# --------------------------------------------------------------------------- #
# CONFIG: QDRANT_URL env override                                             #
# --------------------------------------------------------------------------- #

def test_env_overrides_when_yaml_omits_qdrant_url(tmp_path, monkeypatch):
    """yaml has a memory section but no qdrant_url; env still wins, and the
    default localhost url never leaks through."""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  collection: my_coll\n")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    cfg = load_config(str(cfg_file))
    assert cfg["memory"]["qdrant_url"] == "http://qdrant:6333"
    assert cfg["memory"]["collection"] == "my_coll"


def test_env_override_on_path_none_default_config(monkeypatch):
    """path=None returns framework defaults; env override applies there too and
    leaves the module-level DEFAULT_CONFIG untouched."""
    monkeypatch.setenv("QDRANT_URL", "http://qdrant-host:7777")
    cfg = load_config(None)
    assert cfg["memory"]["qdrant_url"] == "http://qdrant-host:7777"
    # The returned dict is a distinct object, default stays pristine.
    assert cfgmod.DEFAULT_CONFIG["memory"]["qdrant_url"] == "http://localhost:6333"
    assert cfg["memory"] is not cfgmod.DEFAULT_CONFIG["memory"]


def test_env_override_preserves_other_memory_fields(tmp_path, monkeypatch):
    """Overriding qdrant_url must not clobber collection / top_k / thresholds
    that come from the merged default config."""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  qdrant_url: http://localhost:6333\n")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    cfg = load_config(str(cfg_file))
    mem = cfg["memory"]
    assert mem["qdrant_url"] == "http://qdrant:6333"
    # These all flow from DEFAULT_CONFIG and must survive the override.
    default_mem = cfgmod.DEFAULT_CONFIG["memory"]
    assert mem["collection"] == default_mem["collection"]
    assert mem["top_k"] == default_mem["top_k"]
    assert mem["similarity_threshold"] == default_mem["similarity_threshold"]
    assert mem["promotion_threshold_sessions"] == default_mem["promotion_threshold_sessions"]
    assert mem["guideline_max_tokens"] == default_mem["guideline_max_tokens"]


def test_two_sequential_loads_reflect_distinct_env_values(tmp_path, monkeypatch):
    """Two load_config calls under different QDRANT_URL values each reflect the
    env in force at call time -- no caching / cross-contamination."""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  collection: c\n")

    monkeypatch.setenv("QDRANT_URL", "http://first:6333")
    first = load_config(str(cfg_file))
    assert first["memory"]["qdrant_url"] == "http://first:6333"

    monkeypatch.setenv("QDRANT_URL", "http://second:6333")
    second = load_config(str(cfg_file))
    assert second["memory"]["qdrant_url"] == "http://second:6333"
    # The earlier result is a separate object and stays as it was.
    assert first["memory"]["qdrant_url"] == "http://first:6333"


def test_empty_string_qdrant_url_is_no_override(tmp_path, monkeypatch):
    """An empty QDRANT_URL is falsy -> treated as unset; the yaml value wins."""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  qdrant_url: http://from-yaml:6333\n")
    monkeypatch.setenv("QDRANT_URL", "")
    cfg = load_config(str(cfg_file))
    assert cfg["memory"]["qdrant_url"] == "http://from-yaml:6333"


def test_empty_string_qdrant_url_keeps_default_on_path_none(monkeypatch):
    """Empty env + path=None -> the default localhost url is preserved."""
    monkeypatch.setenv("QDRANT_URL", "")
    cfg = load_config(None)
    assert cfg["memory"]["qdrant_url"] == "http://localhost:6333"


def test_env_matching_yaml_value_returns_same_object(tmp_path, monkeypatch):
    """When env equals the already-set url the override is a no-op shortcut and
    the value is identical (still correct, just not re-wrapped)."""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text("memory:\n  qdrant_url: http://same:6333\n")
    monkeypatch.setenv("QDRANT_URL", "http://same:6333")
    cfg = load_config(str(cfg_file))
    assert cfg["memory"]["qdrant_url"] == "http://same:6333"


# --------------------------------------------------------------------------- #
# SYSTEM-PROMPT: policy gating + ordering                                     #
# --------------------------------------------------------------------------- #

def test_file_edit_policy_needs_both_tools():
    """FILE_EDIT_POLICY appears only when BOTH edit_file and write_file are in
    the tool descriptions -- one alone is not enough."""
    only_edit = build_system_prompt(
        context_block="", tool_descriptions="- edit_file: edit a file"
    )
    assert FILE_EDIT_POLICY not in only_edit

    only_write = build_system_prompt(
        context_block="", tool_descriptions="- write_file: write a file"
    )
    assert FILE_EDIT_POLICY not in only_write

    both = build_system_prompt(
        context_block="",
        tool_descriptions="- edit_file: edit a file\n- write_file: write a file",
    )
    assert FILE_EDIT_POLICY in both


def test_ordering_prefix_before_identity_before_tools():
    out = build_system_prompt(
        context_block="",
        tool_descriptions="- read_file: read it",
        system_prompt="You are the map_agent.",
        system_prompt_prefix="GLOBAL PREFIX NOTE",
    )
    i_prefix = out.index("GLOBAL PREFIX NOTE")
    i_identity = out.index("You are the map_agent.")
    i_tools = out.index("Available tools:")
    assert i_prefix < i_identity < i_tools


def test_toon_note_only_on_toon_result_format():
    json_out = build_system_prompt(
        context_block="", tool_descriptions="- read_file: x", result_format="json"
    )
    assert TOON_RESULT_NOTE not in json_out

    toon_out = build_system_prompt(
        context_block="", tool_descriptions="- read_file: x", result_format="toon"
    )
    assert TOON_RESULT_NOTE in toon_out


def test_system_prompt_override_still_gets_all_gated_policies():
    """A wholesale identity override must NOT suppress the gated policies when
    their tools are present."""
    desc = (
        "- edit_file: edit\n- write_file: write\n"
        "- spawn_subagent: spawn a child\n- python_exec: run code"
    )
    out = build_system_prompt(
        context_block="ctx",
        tool_descriptions=desc,
        system_prompt="You are a fully custom agent.",
        result_format="toon",
    )
    assert "You are a fully custom agent." in out
    assert DEFAULT_AGENT_IDENTITY not in out  # override replaces it
    assert FILE_EDIT_POLICY in out
    assert FRUGALITY_POLICY in out
    assert DELEGATION_POLICY in out
    assert CODE_ACTION_POLICY in out
    assert TOON_RESULT_NOTE in out


def test_context_block_appears_last():
    desc = (
        "- edit_file: edit\n- write_file: write\n"
        "- spawn_subagent: spawn\n- batch: many"
    )
    ctx = "CONTEXT_SENTINEL_BLOCK"
    out = build_system_prompt(
        context_block=ctx,
        tool_descriptions=desc,
        result_format="toon",
    )
    # Everything that could be appended sits before the context block.
    for earlier in (
        DEFAULT_AGENT_IDENTITY,
        "Available tools:",
        FILE_EDIT_POLICY,
        FRUGALITY_POLICY,
        DELEGATION_POLICY,
        CODE_ACTION_POLICY,
        TOON_RESULT_NOTE,
    ):
        assert out.index(earlier) < out.index(ctx)
    assert out.rstrip().endswith(ctx)


def test_empty_tool_descriptions_omits_available_tools_block():
    out = build_system_prompt(context_block="", tool_descriptions="")
    assert "Available tools:" not in out
    # Identity + always-on frugality still present.
    assert DEFAULT_AGENT_IDENTITY in out
    assert FRUGALITY_POLICY in out


def test_code_action_gated_on_batch_alone():
    """CODE_ACTION_POLICY fires on `batch` even without python_exec."""
    out = build_system_prompt(
        context_block="", tool_descriptions="- batch: run many calls"
    )
    assert CODE_ACTION_POLICY in out


# --------------------------------------------------------------------------- #
# SPAWN: build_runner_command plumbing                                        #
# --------------------------------------------------------------------------- #

def _write_cfg(tmp_path: Path, collection: str | None) -> Path:
    cfg = tmp_path / "agent.yaml"
    if collection is None:
        cfg.write_text("agent:\n  name: child\n")
    else:
        cfg.write_text(f"memory:\n  collection: {collection}\n")
    return cfg


def test_suffix_sanitization_mixed_case_and_punctuation():
    assert sanitize_collection_suffix("My-Tile.Map/v2!") == "my-tilemapv2"
    assert sanitize_collection_suffix("UPPER_lower-123") == "upper_lower-123"


def test_suffix_sanitization_caps_at_32_chars():
    raw = "a-b_c" * 20  # 100 chars, all allowed
    out = sanitize_collection_suffix(raw)
    assert len(out) == 32
    assert out == raw[:32]


def test_suffix_that_sanitizes_to_empty_omits_flag(tmp_path):
    """A suffix made entirely of disallowed chars sanitizes to '' -> no
    --memory-collection flag is emitted."""
    cfg = _write_cfg(tmp_path, "parent_coll")
    cmd = build_runner_command({
        "config_path": str(cfg), "task": "t",
        "memory_collection_suffix": "!!!///...",
    })
    assert "--memory-collection" not in cmd


def test_suffix_concats_against_default_collection_when_yaml_omits_it(tmp_path):
    """Valid suffix and a yaml WITHOUT an explicit memory.collection: load_config
    merges DEFAULT_CONFIG, so the parent collection resolves to the framework
    default (COLLECTION_NAME) and the flag IS emitted -- it is never None via
    load_config. This documents that the merge means there's always a parent."""
    from fabri.memory.store import COLLECTION_NAME

    cfg = _write_cfg(tmp_path, None)
    cmd = build_runner_command({
        "config_path": str(cfg), "task": "t",
        "memory_collection_suffix": "tile",
    })
    idx = cmd.index("--memory-collection") + 1
    assert cmd[idx] == f"{COLLECTION_NAME}_tile"


def test_suffix_omits_flag_when_parent_collection_unreadable(tmp_path):
    """If the parent collection cannot be read (config_path points at a missing
    file -> load_config raises -> _parent_collection returns None), no flag is
    emitted even with a valid suffix."""
    missing = tmp_path / "does_not_exist.yaml"
    cmd = build_runner_command({
        "config_path": str(missing), "task": "t",
        "memory_collection_suffix": "tile",
    })
    assert "--memory-collection" not in cmd


def test_config_path_resolved_to_absolute(tmp_path, monkeypatch):
    """A relative config_path becomes absolute in the command (argv[2])."""
    cfg = _write_cfg(tmp_path, "parent")
    monkeypatch.chdir(tmp_path)
    cmd = build_runner_command({"config_path": "agent.yaml", "task": "t"})
    config_arg = cmd[2]
    assert Path(config_arg).is_absolute()
    assert Path(config_arg) == cfg.resolve()
    assert cmd[0] == sys.executable


def test_system_prompt_path_resolved_to_absolute(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, "parent")
    prompt = tmp_path / "sub" / "prompt.md"
    prompt.parent.mkdir()
    prompt.write_text("be terse")
    monkeypatch.chdir(tmp_path)
    cmd = build_runner_command({
        "config_path": str(cfg), "task": "t",
        "system_prompt_path": "sub/prompt.md",
    })
    idx = cmd.index("--system-prompt-file") + 1
    assert Path(cmd[idx]).is_absolute()
    assert Path(cmd[idx]) == prompt.resolve()


def test_runner_script_override_lands_in_argv(tmp_path):
    """Explicit runner_script arg is used verbatim as argv[1]."""
    cfg = _write_cfg(tmp_path, "parent")
    fake = tmp_path / "fake_runner.py"
    fake.write_text("# noop\n")
    cmd = build_runner_command(
        {"config_path": str(cfg), "task": "t"}, runner_script=fake
    )
    assert cmd[1] == str(fake)


def test_both_prompt_forms_raise_valueerror(tmp_path):
    cfg = _write_cfg(tmp_path, "parent")
    with pytest.raises(ValueError):
        build_runner_command({
            "config_path": str(cfg), "task": "t",
            "system_prompt_inline": "inline",
            "system_prompt_path": "/tmp/p.md",
        })
