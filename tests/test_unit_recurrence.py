"""Unit coverage for pure ActionMemory recurrence matching."""
from __future__ import annotations

import pytest

from fabri.memory.recurrence import applicable, apply_confidence, canonicalize, fingerprint

pytestmark = pytest.mark.unit


def _revenue_ops_resolution() -> dict[str, object]:
    return {
        "problem_signature": {
            "phase": "training",
            "agency": "market-research-brief",
            "roles": ["researcher", "writer"],
            "error_class": "LLMError",
            "cause": "truncation",
            "configured_cap": 768,
            "retry_cap": 1536,
            "model_family": "gpt-5.6-terra",
            "finish_reason": "length",
        },
        "scope": {
            "company": "Revenue Ops",
            "agency": "market-research-brief",
            "roles": ["researcher", "writer"],
        },
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
        "postconditions": [{"field": "roles_config.researcher.max_tokens", "equals": 2048}],
        "rollback": {"capability": "restore_role_config"},
        "evidence": {"source_session_ids": ["source-1"], "verification": "replayed", "replays": 1},
        "policy": {"idempotent": True, "max_attempts": 1, "approval": "required", "expiry": None},
    }


def _matching_state() -> dict[str, object]:
    return {
        "company": "Revenue Ops",
        "agency": "market-research-brief",
        "roles": ["writer", "researcher"],
        "phase": "training",
        "error_class": "LLMError",
        "cause": "truncation",
        "configured_cap": 768,
        "retry_cap": 1536,
        "model_family": "gpt-5.6-terra",
        "finish_reason": "length",
        "roles_config": {
            "researcher": {"max_tokens": 768},
            "writer": {"max_tokens": 768},
        },
        "task": "Produce the Revenue Ops market research brief.",
    }


def test_fingerprint_ignores_volatile_signals_but_distinguishes_hard_fields() -> None:
    first = {
        **_matching_state(),
        "session_id": "session-one",
        "timestamp": "2026-07-22T10:00:00Z",
        "absolute_path": "/tmp/one",
        "price_usd": 3.14,
        "free_text": "writer output was short",
    }
    second = {
        **_matching_state(),
        "session_id": "session-two",
        "timestamp": "2026-07-23T10:00:00Z",
        "absolute_path": "/tmp/two",
        "price_usd": 9.99,
        "free_text": "different wording",
    }
    different_agency = {**second, "agency": "another-agency"}

    assert fingerprint(canonicalize(first)) == fingerprint(canonicalize(second))
    assert fingerprint(canonicalize(first)) != fingerprint(canonicalize(different_agency))


def test_revenue_ops_resolution_is_applicable_for_the_still_bad_config() -> None:
    assert applicable(_revenue_ops_resolution(), _matching_state()) is True


@pytest.mark.parametrize(
    "state",
    [
        {**_matching_state(), "agency": "another-agency"},
        {
            **_matching_state(),
            "roles_config": {
                "researcher": {"max_tokens": 768},
                "writer": {"max_tokens": 2048},
            },
        },
        {**_matching_state(), "cause": "timeout"},
        {
            **_matching_state(),
            "roles_config": {"researcher": {"max_tokens": 768}},
        },
    ],
)
def test_revenue_ops_resolution_refuses_inapplicable_states(state: dict[str, object]) -> None:
    assert applicable(_revenue_ops_resolution(), state) is False


def test_apply_confidence_requires_exact_recurrence_and_hard_preconditions() -> None:
    resolution = _revenue_ops_resolution()
    exact = apply_confidence(resolution, _matching_state(), retrieval_relevance=0.8)
    timeout = apply_confidence(
        resolution, {**_matching_state(), "cause": "timeout"}, retrieval_relevance=0.99
    )
    vague_short_output = apply_confidence(
        resolution,
        {
            "company": "Revenue Ops",
            "agency": "market-research-brief",
            "roles_config": {"writer": {"max_tokens": 768}},
            "task": "writer output was short",
        },
        retrieval_relevance=0.99,
    )

    assert exact >= 0.9
    assert timeout < 0.2
    assert vague_short_output < 0.2
