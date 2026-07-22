from __future__ import annotations

from pathlib import Path

import pytest

from fabri.memory.schema import MemoryEntry
from fabri.orchestrator.action_detection import (
    build_current_state,
    derive_scope_from_collection,
    detect_proposed_actions,
    resolve_action_scope,
)

pytestmark = pytest.mark.unit


class MemoryStore:
    def __init__(self, entries: list[MemoryEntry]) -> None:
        self.entries = {entry.id: entry for entry in entries}

    def iterate(self, kind: str | None = None, limit: int | None = None) -> list[MemoryEntry]:
        entries = [entry for entry in self.entries.values() if kind is None or entry.kind == kind]
        return entries[:limit] if limit is not None else entries


def _resolution() -> dict:
    return {
        "problem_signature": {"configured_cap": 768},
        "scope": {
            "company": "revenue-ops",
            "agency": "market-research-brief",
            "roles": ["researcher", "writer"],
        },
        "preconditions": [
            {"field": "roles_config.researcher.max_tokens", "equals": 768},
            {"field": "roles_config.writer.max_tokens", "equals": 768},
        ],
        "steps": [
            {"capability": "configure_role", "args_template": {"role": "researcher", "max_tokens": 2048}},
            {"capability": "configure_role", "args_template": {"role": "writer", "max_tokens": 2048}},
        ],
    }


def _store(resolution: dict) -> MemoryStore:
    return MemoryStore([MemoryEntry(text="resolution", kind="success_pattern", resolution=resolution)])


def _write_child_config(tmp_path: Path, name: str, max_tokens: int) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(f"llm:\n  max_tokens: {max_tokens}\n")
    return path


def _entries(tmp_path: Path, max_tokens: int) -> list[dict[str, str]]:
    return [
        {"name": "researcher", "config": str(_write_child_config(tmp_path, "researcher", max_tokens))},
        {"name": "writer", "config": str(_write_child_config(tmp_path, "writer", max_tokens))},
    ]


def test_detects_action_for_matching_manager_child_configs(tmp_path: Path) -> None:
    resolution = _resolution()

    proposals = detect_proposed_actions(
        _store(resolution),
        _entries(tmp_path, 768),
        "Produce the Revenue Ops market research brief.",
        company="revenue-ops",
        agency="market-research-brief",
    )

    assert proposals == [resolution]


@pytest.mark.parametrize(
    ("max_tokens", "company"),
    [(2048, "revenue-ops"), (768, "another-company")],
)
def test_refuses_inapplicable_manager_child_configs(
    tmp_path: Path, max_tokens: int, company: str
) -> None:
    assert detect_proposed_actions(
        _store(_resolution()),
        _entries(tmp_path, max_tokens),
        "Produce the Revenue Ops market research brief.",
        company=company,
        agency="market-research-brief",
    ) == []


def test_skips_unloadable_child_config_without_preventing_detection(tmp_path: Path) -> None:
    resolution = _resolution()
    entries = _entries(tmp_path, 768)
    entries.append({"name": "stale", "config": str(tmp_path / "missing.yaml")})

    assert detect_proposed_actions(
        _store(resolution),
        entries,
        "Produce the Revenue Ops market research brief.",
        company="revenue-ops",
        agency="market-research-brief",
    ) == [resolution]


def test_derive_scope_from_collection_is_best_effort() -> None:
    assert derive_scope_from_collection("acme_researcher") == ("acme", "researcher")
    assert derive_scope_from_collection(None) == (None, None)


def test_resolve_action_scope_prefers_compiler_metadata() -> None:
    assert resolve_action_scope({
        "collection": "revenue_ops_market_research_brief_parent",
        "action_scope": {
            "company": "revenue_ops",
            "agency": "market_research_brief",
        },
    }) == ("revenue_ops", "market_research_brief")


def test_build_current_state_uses_child_node_names_and_caps(tmp_path: Path) -> None:
    state = build_current_state(
        _entries(tmp_path, 768),
        "Produce the Revenue Ops market research brief.",
        company="revenue-ops",
        agency="market-research-brief",
    )

    assert state == {
        "company": "revenue-ops",
        "agency": "market-research-brief",
        "roles_config": {
            "researcher": {"max_tokens": 768},
            "writer": {"max_tokens": 768},
        },
        "task": "Produce the Revenue Ops market research brief.",
    }
