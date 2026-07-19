"""build_system_prompt frugality stance: the deliberation policy is always
present; the delegation and code-as-action policies are registry-gated so they
never reference a tool the agent doesn't actually have."""
from fabri.core.agent import (
    CODE_ACTION_POLICY,
    DELEGATION_POLICY,
    FRUGALITY_POLICY,
    RETRIEVED_GUIDELINES_TASK_PRECEDENCE,
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


def test_task_requirements_precedence_follows_retrieved_guidelines_by_default():
    context = "<retrieved_guidelines>past hint</retrieved_guidelines>"

    out = build_system_prompt(
        context_block=context,
        tool_descriptions="- read_file: read a file",
    )

    assert out.index(context) < out.index(RETRIEVED_GUIDELINES_TASK_PRECEDENCE)
    assert out.rstrip().endswith(RETRIEVED_GUIDELINES_TASK_PRECEDENCE)


def test_task_requirements_precedence_can_be_disabled_for_compatibility():
    out = build_system_prompt(
        context_block="<retrieved_guidelines>past hint</retrieved_guidelines>",
        tool_descriptions="- read_file: read a file",
        retrieved_guidelines_task_precedence=False,
    )

    assert RETRIEVED_GUIDELINES_TASK_PRECEDENCE not in out
