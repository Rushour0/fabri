"""Unit coverage for deterministic truncation ActionMemory mining."""
from __future__ import annotations

import uuid

import pytest

from fabri.memory.action_mining import (
    action_candidate_text,
    build_truncation_action_candidate,
    observed_max_token_retries,
)
from fabri.memory.pruning import ingest_guideline
from fabri.memory.recurrence import applicable
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore

pytestmark = pytest.mark.unit


def _config() -> dict:
    return {
        "llm": {"model": "anthropic/claude-test", "max_tokens": 768},
        "memory": {"collection": "acme_researcher"},
        "agent": {"name": "researcher"},
    }


def test_builds_well_formed_candidate_only_for_truncation_retries() -> None:
    candidate = build_truncation_action_candidate(_config(), 1, "failed", "Research competitors")

    assert candidate is not None
    assert candidate["problem_signature"] == {
        "phase": "run",
        "agency": "researcher",
        "roles": ["researcher"],
        "error_class": "LLMError",
        "cause": "truncation",
        "configured_cap": 768,
        "retry_cap": 1536,
        "model_family": "anthropic/claude-test",
        "finish_reason": "length",
    }
    assert candidate["policy"] == {"idempotent": True, "max_attempts": 1, "approval": "shadow"}
    assert build_truncation_action_candidate(_config(), 0, "failed", "Research competitors") is None
    assert build_truncation_action_candidate({"memory": {}}, 1, "failed", "Research competitors") is None


def test_mined_candidate_matches_only_while_role_has_bad_cap() -> None:
    candidate = build_truncation_action_candidate(_config(), 1, "failed", "Research competitors")

    assert candidate is not None
    matching_state = {
        "company": "acme",
        "agency": "researcher",
        "roles_config": {"researcher": {"max_tokens": 768}},
    }
    fixed_state = {
        "company": "acme",
        "agency": "researcher",
        "roles_config": {"researcher": {"max_tokens": 1536}},
    }
    assert applicable(candidate, matching_state)
    assert not applicable(candidate, fixed_state)


def test_compiler_scope_overrides_ambiguous_collection_parsing() -> None:
    config = _config()
    config["memory"]["collection"] = "revenue_ops_market_research_brief_researcher"
    config["memory"]["action_scope"] = {
        "company": "revenue_ops",
        "agency": "market_research_brief",
        "role": "researcher",
    }

    candidate = build_truncation_action_candidate(config, 1, "success", "Research market")

    assert candidate is not None
    assert candidate["scope"] == {
        "company": "revenue_ops",
        "agency": "market_research_brief",
        "roles": ["researcher"],
    }


def test_observed_retries_include_the_real_run_not_only_compression() -> None:
    assert observed_max_token_retries(
        {"usage": {"max_token_retries": 2}},
        post_run_max_token_retries=1,
    ) == 3
    assert observed_max_token_retries(
        {"usage": {"max_token_retries": True}},
        post_run_max_token_retries=-1,
    ) == 0


def test_action_text_is_stable_and_role_specific() -> None:
    researcher = {"scope": {"roles": ["researcher"]}}
    writer = {"scope": {"roles": ["writer"]}}

    assert action_candidate_text(researcher) == (
        "Increase researcher token cap after a truncation retry."
    )
    assert action_candidate_text(writer) != action_candidate_text(researcher)


def test_explicit_tier_and_resolution_round_trip_without_changing_id() -> None:
    candidate = build_truncation_action_candidate(_config(), 1, "failed", "Research competitors")
    assert candidate is not None
    store = QdrantMemoryStore(collection=f"action_mining_{uuid.uuid4().hex}")
    text = "Increase the researcher cap after a truncation retry."
    expected_id = MemoryEntry(text=text, kind="success_pattern").id

    stored = ingest_guideline(
        store,
        text,
        session_id="session-1",
        kind="success_pattern",
        tier="quarantine",
        resolution=candidate,
    )
    restored = store.get(stored.id)

    assert stored.id == expected_id
    assert restored is not None
    assert restored.tier == "quarantine"
    assert restored.resolution == candidate


def test_existing_ingest_callers_keep_default_tier_and_resolution() -> None:
    store = QdrantMemoryStore(collection=f"action_mining_{uuid.uuid4().hex}")
    stored = ingest_guideline(store, "Keep existing ingestion behavior.", session_id="session-1")

    assert stored.tier == "unclassified"
    assert stored.resolution is None
