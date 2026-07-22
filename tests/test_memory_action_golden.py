from __future__ import annotations

import pytest

from fabri.core.agent import PROPOSED_ACTION_NOTE, build_system_prompt
from fabri.memory.recurrence import apply_confidence
from fabri.memory.schema import MemoryEntry
from fabri.orchestrator import retrieval
from fabri.orchestrator.retrieval import (
    RetrievalConfig,
    propose_actions,
    retrieve_context_with_meta,
)

pytestmark = pytest.mark.unit


class MemoryStore:
    def __init__(self, entries: list[MemoryEntry]) -> None:
        self.entries = {entry.id: entry for entry in entries}

    def iterate(self, kind: str | None = None, limit: int | None = None) -> list[MemoryEntry]:
        entries = [entry for entry in self.entries.values() if kind is None or entry.kind == kind]
        return entries[:limit] if limit is not None else entries

    def count(self) -> int:
        return len(self.entries)


def _resolution(*, agency: str = "market-research-brief") -> dict:
    return {
        "problem_signature": {
            "phase": "training", "agency": agency, "roles": ["researcher", "writer"],
            "error_class": "LLMError", "cause": "truncation", "configured_cap": 768,
            "retry_cap": 1536, "model_family": "gpt-5.6-terra", "finish_reason": "length",
        },
        "scope": {"company": "revenue-ops", "agency": agency, "roles": ["researcher", "writer"]},
        "preconditions": [
            {"field": "cause", "equals": "truncation"},
            {"field": "error_class", "equals": "LLMError"},
            {"field": "roles_config.researcher.max_tokens", "equals": 768},
            {"field": "roles_config.writer.max_tokens", "equals": 768},
        ],
        "steps": [
            {"capability": "configure_role", "args_template": {"role": "researcher", "max_tokens": 2048}},
            {"capability": "configure_role", "args_template": {"role": "writer", "max_tokens": 2048}},
        ],
    }


def _state(**overrides: object) -> dict:
    return {
        "company": "revenue-ops", "agency": "market-research-brief",
        "phase": "training", "roles": ["researcher", "writer"], "error_class": "LLMError",
        "cause": "truncation", "configured_cap": 768, "retry_cap": 1536,
        "model_family": "gpt-5.6-terra", "finish_reason": "length",
        "roles_config": {"researcher": {"max_tokens": 768}, "writer": {"max_tokens": 768}},
        "task": "Produce the Revenue Ops market research brief.",
        **overrides,
    }


def _store(*resolutions: dict) -> MemoryStore:
    return MemoryStore([
        MemoryEntry(text=f"resolution {index}", kind="success_pattern", resolution=resolution)
        for index, resolution in enumerate(resolutions)
    ])


def test_revenue_ops_action_is_detected_and_proposed_with_high_confidence() -> None:
    resolution = _resolution()
    current_state = _state()

    assert propose_actions(_store(resolution), current_state) == [resolution]
    assert apply_confidence(resolution, current_state, 1.0) >= 0.9


def test_promoted_action_remains_executable() -> None:
    resolution = _resolution()
    entry = MemoryEntry(text="promoted recovery", kind="strategic", resolution=resolution)

    assert propose_actions(MemoryStore([entry]), _state()) == [resolution]


@pytest.mark.parametrize(
    "current_state, resolution",
    [
        (_state(roles_config={"researcher": {"max_tokens": 2048}, "writer": {"max_tokens": 2048}}), _resolution()),
        (_state(agency="another-agency"), _resolution()),
        (_state(cause="timeout"), _resolution()),
        (_state(), _resolution(agency="another-agency")),
    ],
)
def test_proposal_refuses_inapplicable_states(current_state: dict, resolution: dict) -> None:
    assert propose_actions(_store(resolution), current_state) == []


def test_surfaces_actions_only_when_flag_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: False)
    store = _store(_resolution())
    current_state = _state()

    _, disabled_meta = retrieve_context_with_meta(
        store, "task", retrieval_config=RetrievalConfig(), current_state=current_state
    )
    _, enabled_meta = retrieve_context_with_meta(
        store, "task", retrieval_config=RetrievalConfig(memory_action_enabled=True), current_state=current_state
    )

    assert "proposed_actions" not in disabled_meta
    assert enabled_meta["proposed_actions"] == [_resolution()]


def test_prompt_adds_proposal_block_only_when_actions_exist() -> None:
    without_actions = build_system_prompt("context", "")
    with_actions = build_system_prompt("context", "", proposed_actions=[_resolution()])

    assert build_system_prompt("context", "", proposed_actions=[]) == without_actions
    assert PROPOSED_ACTION_NOTE.split("\n", 1)[0] not in without_actions
    assert "configure_role({'role': 'researcher', 'max_tokens': 2048})" in with_actions


def _live_agent_current_state() -> dict:
    """The shape `agent.py::_run_single_attempt` actually builds today: keyed by
    the request's LLM backend SLOTS (main/decompose/...), with NO company/agency.
    A single agent cannot see sibling agency-role config, so this documents the
    live integration gap -- config-precondition detection belongs at the
    orchestration/compile layer, not inside one agent's run."""
    return {
        "roles_config": {"main": {"max_tokens": 768}, "decompose": {"max_tokens": 768}},
        "task": "Produce the Revenue Ops market research brief.",
    }


def test_live_agent_current_state_cannot_match_today() -> None:
    # Honest guard (code-review finding): the golden tests above hand-build a
    # current_state shape the real agent loop never produces. Through the actual
    # _run_single_attempt-built state, no config-precondition action can match,
    # because company/agency are absent and roles are keyed by backend slot.
    assert propose_actions(_store(_resolution()), _live_agent_current_state()) == []
