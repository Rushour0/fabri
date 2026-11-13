"""Prompt-kit task brief (D3) and the specialist-profile factory (D4)."""
import json

import pytest

from fabri.builder.prompt_kit import render_prompt_template, render_task_brief
from fabri.builder.specialists import (
    ProfileCache,
    SpecialistError,
    make_specialist,
    profile_cache_key,
    spawn_args,
)
from fabri.core.llm import LLMResponse

PARENT_TOOLS = ["bash", "edit_file", "grep", "read_file", "write_file"]


class _CountingLLM:
    def __init__(self, reply: dict | str):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls = 0
        self.prompts: list[str] = []

    def step(self, system, messages):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        return LLMResponse(final_text=self.reply)


_REPLY = {"role": "test failure triager", "owns": ["a ranked list of likely causes"],
          "tools": ["read_file", "grep", "deploy_prod"],
          "tool_notes": {"grep": "find where the failing symbol is defined"}}


# --- D3 -------------------------------------------------------------------

def test_task_brief_has_the_four_parts_in_order():
    brief = render_task_brief("Find why test_x fails.", tools=["grep", "read_file"],
                              tool_notes={"grep": "search code"}, boundaries=["Do not edit files."])
    heads = ["## Objective", "## Output format", "## Tools", "## Boundaries"]
    pos = [brief.index(h) for h in heads]
    assert pos == sorted(pos)
    assert "Find why test_x fails." in brief
    assert "- grep -- search code" in brief and "- read_file" in brief
    assert "- Do not edit files." in brief


def test_task_brief_is_deterministic_and_requires_objective():
    assert render_task_brief("x", tools=["a"]) == render_task_brief("x", tools=["a"])
    with pytest.raises(ValueError):
        render_task_brief("  ")
    assert "no tools" in render_task_brief("x")


def test_skeleton_no_longer_shouts():
    text = render_prompt_template("agent")
    assert "ABSOLUTE" not in text and "HARD INVARIANTS" not in text
    assert "# WORKING RULES" in text and "# SCOPE" in text
    # Rules carry their reasons.
    assert "because" in text


# --- D4 -------------------------------------------------------------------

def test_factory_narrows_to_parent_tools_and_renders_prompt():
    llm = _CountingLLM(_REPLY)
    p = make_specialist("Triage the failing test_x", PARENT_TOOLS, llm)
    assert p.tools == ("grep", "read_file")  # deploy_prod dropped, sorted
    assert "test failure triager" in p.system_prompt
    assert "- grep -- find where the failing symbol is defined" in p.system_prompt
    assert "deploy_prod" not in p.system_prompt
    assert p.name.startswith("test_failure_triager_")


def test_factory_cache_hit_skips_llm_and_is_stable(tmp_path):
    cache = ProfileCache(tmp_path)
    llm = _CountingLLM(_REPLY)
    a = make_specialist("Triage test_x", PARENT_TOOLS, llm, task_kind="triage failing test", cache=cache)
    b = make_specialist("Triage test_y", PARENT_TOOLS, llm, task_kind="Triage  failing TEST", cache=cache)
    assert llm.calls == 1 and a == b
    # A fresh cache on the same dir loads from disk without the LLM.
    c = make_specialist("Triage test_z", list(reversed(PARENT_TOOLS)), None,
                        task_kind="triage failing test", cache=ProfileCache(tmp_path))
    assert c == a


def test_cache_key_changes_with_tools_or_kind():
    k = profile_cache_key("kind", ["a", "b"])
    assert k == profile_cache_key("kind", ["b", "a", "a"])
    assert k != profile_cache_key("kind", ["a"])
    assert k != profile_cache_key("other", ["a", "b"])


def test_factory_errors():
    with pytest.raises(SpecialistError, match="no tool the parent has"):
        make_specialist("t", PARENT_TOOLS, _CountingLLM({"role": "r", "tools": ["nope"]}))
    with pytest.raises(SpecialistError, match="no usable profile"):
        make_specialist("t", PARENT_TOOLS, _CountingLLM("not json"))
    with pytest.raises(SpecialistError, match="no LLM"):
        make_specialist("t", PARENT_TOOLS, None)


def test_designer_sees_only_tool_names_from_code():
    llm = _CountingLLM(_REPLY)
    make_specialist("Triage", PARENT_TOOLS, llm)
    for t in PARENT_TOOLS:
        assert f"- {t}" in llm.prompts[0]


def test_spawn_args_set_scope_from_code_and_keep_prompt_stable():
    p = make_specialist("Triage", PARENT_TOOLS, _CountingLLM(_REPLY))
    a1 = spawn_args(p, "Why does test_x fail?", "/cfg/agent.yaml", parallel_group="g")
    a2 = spawn_args(p, "Why does test_y fail?", "/cfg/agent.yaml")
    assert a1["tool_scope"] == ["grep", "read_file"]
    assert a1["system_prompt_inline"] == a2["system_prompt_inline"]  # cacheable
    assert "Why does test_x fail?" in a1["task"] and "## Boundaries" in a1["task"]
    assert a1["parallel_group"] == "g"
