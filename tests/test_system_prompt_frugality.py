"""build_system_prompt frugality stance: the deliberation policy is always
present; the delegation and code-as-action policies are registry-gated so they
never reference a tool the agent doesn't actually have."""
from fabri.core.agent import (
    CODE_ACTION_POLICY,
    DELEGATION_POLICY,
    FRUGALITY_POLICY,
    build_system_prompt,
)


def test_frugality_always_present_even_with_custom_identity():
    # A domain config that replaces the identity wholesale still gets the
    # frugality stance appended -- cost discipline isn't opt-out.
    out = build_system_prompt(
        context_block="",
        tool_descriptions="- read_file: read a file",
        system_prompt="You are the story_agent.",
    )
    assert "You are the story_agent." in out
    assert FRUGALITY_POLICY in out


def test_delegation_policy_gated_on_spawn_subagent():
    without = build_system_prompt(context_block="", tool_descriptions="- read_file: x")
    assert DELEGATION_POLICY not in without
    with_spawn = build_system_prompt(
        context_block="", tool_descriptions="- spawn_subagent: spawn a child"
    )
    assert DELEGATION_POLICY in with_spawn


def test_code_action_policy_gated_on_code_or_batch_tool():
    without = build_system_prompt(context_block="", tool_descriptions="- read_file: x")
    assert CODE_ACTION_POLICY not in without
    for desc in ("- python_exec: run code", "- batch: run many calls"):
        out = build_system_prompt(context_block="", tool_descriptions=desc)
        assert CODE_ACTION_POLICY in out
