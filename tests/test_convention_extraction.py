from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from fabri.core.llm import LLMResponse
from fabri.memory.convention_extraction import extract_convention_candidates
from fabri.memory.schema import MemoryEntry
from fabri.orchestrator import pipeline
from fabri.orchestrator.pipeline import process_trace

pytestmark = pytest.mark.unit


class MemoryStore:
    def __init__(self) -> None:
        self.entries: dict[str, MemoryEntry] = {}

    def upsert(self, entry: MemoryEntry) -> str:
        self.entries[entry.id] = entry
        return entry.id

    def delete(self, point_id: str) -> None:
        self.entries.pop(point_id, None)

    def iterate(
        self,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        entries = [
            entry
            for entry in self.entries.values()
            if kind is None or entry.kind == kind
        ]
        return entries[:limit] if limit is not None else entries


class MockLLM:
    def __init__(self, final_text: str) -> None:
        self.final_text = final_text
        self.calls = 0
        self.system = ""
        self.messages: list[dict] = []

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        self.calls += 1
        self.system = system
        self.messages = messages
        return LLMResponse(final_text=self.final_text)


def _config(*, enabled: bool = True) -> dict[str, object]:
    return {
        "memory": {
            "convention_mining_enabled": enabled,
            "convention_trusted_sources": [],
            "convention_approvals": [],
            "convention_allowed_effect_classes": ["response_mapping"],
            "convention_max_tokens": 384,
            "convention_max_branches": 8,
            "convention_max_entries": 256,
            "convention_default_ttl_days": 180,
        }
    }


def _candidate(
    *,
    conditions: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "source_id": "event:0:task",
        "scope": "company",
        "key": "SHQ-E1",
        "version": "1",
        "effect_class": "response_mapping",
        "conditions": conditions or [
            {
                "branch_id": "report-only",
                "condition_text": "Impact is limited to one customer and no rollback occurred.",
            },
            {
                "branch_id": "mitigated",
                "condition_text": (
                    "Multiple customers were impacted, rollback restored baseline, "
                    "and no permanent remedy exists."
                ),
            },
        ],
        "branches": [
            {
                "branch_id": "report-only",
                "fields": {
                    "status": "OPEN",
                    "audience": "INTERNAL",
                    "disclosure": "CLASS_ONLY",
                    "update": "MONITOR_UPDATE",
                },
            },
            {
                "branch_id": "mitigated",
                "fields": {
                    "status": "MITIGATED",
                    "audience": "EXTERNAL_STATUS",
                    "disclosure": "CLASS_ONLY",
                    "update": "MONITOR_UPDATE",
                },
            },
        ],
        "response_schema": {
            "status": "string",
            "audience": "string",
            "disclosure": "string",
            "update": "string",
        },
    }


def _response(candidate: Mapping[str, object]) -> str:
    return json.dumps({"candidates": [candidate]})


@pytest.fixture(autouse=True)
def _pipeline_without_external_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    monkeypatch.setattr(pipeline, "embeddings_available", lambda: True)
    monkeypatch.setattr(pipeline, "_emit_mining_report", lambda report, callback: None)


def test_well_formed_two_branch_protocol_is_extracted_and_ingested_quarantined() -> None:
    llm = MockLLM(_response(_candidate()))
    store = MemoryStore()
    events = [
        {
            "type": "start",
            "task": "Use SHQ-E1: choose report-only or mitigated incident fields.",
        }
    ]

    entries = process_trace(
        "session-shq",
        store,
        llm,
        events=events,
        config=_config(),
    )

    assert llm.calls == 1
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "convention"
    assert entry.tier == "quarantine"
    assert entry.verification == "unverified"
    assert entry.payload["quarantine_reason"] == "approval_required"
    stored_record = entry.payload["record"]
    assert isinstance(stored_record, dict)
    assert stored_record["origin"] == "task"
    assert stored_record["provenance"] == "session:session-shq:event:0:task"
    assert stored_record["branches"][1]["fields"] == {
        "status": "MITIGATED",
        "audience": "EXTERNAL_STATUS",
        "disclosure": "CLASS_ONLY",
        "update": "MONITOR_UPDATE",
    }
    assert "STRICT JSON SCHEMA" in llm.messages[0]["content"]


def test_malformed_json_returns_no_candidate_without_exception() -> None:
    llm = MockLLM("{not-json")

    candidates = extract_convention_candidates(
        ["SHQ-E1 has two response branches."],
        llm,
        config=_config(),
    )

    assert candidates == []
    assert llm.calls == 1


def test_duplicate_normalized_conditions_are_quarantined_whole() -> None:
    duplicate_conditions = [
        {"branch_id": "report-only", "condition_text": "  INCIDENT is mitigated. "},
        {"branch_id": "mitigated", "condition_text": "incident IS mitigated"},
    ]
    llm = MockLLM(_response(_candidate(conditions=duplicate_conditions)))
    store = MemoryStore()

    entries = process_trace(
        "session-duplicate",
        store,
        llm,
        events=[{"type": "start", "task": "Apply the incident protocol."}],
        config=_config(),
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.tier == "quarantine"
    assert entry.payload["quarantine_reason"] == "convention_mining_disabled"
    stored_record = entry.payload["record"]
    assert isinstance(stored_record, dict)
    assert stored_record["conditions"] == duplicate_conditions
    assert str(stored_record["provenance"]).endswith("gate=duplicate_conditions")


def test_flag_off_never_calls_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_extraction(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("extractor must stay off")

    monkeypatch.setattr(pipeline, "extract_convention_candidates", unexpected_extraction)
    store = MemoryStore()

    entries = process_trace(
        "session-off",
        store,
        MockLLM("unused"),
        events=[{"type": "start", "task": "No mining."}],
        config=_config(enabled=False),
    )

    assert entries == []
    assert store.entries == {}


def test_extraction_exception_does_not_affect_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_extraction(*args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(pipeline, "extract_convention_candidates", broken_extraction)
    store = MemoryStore()

    entries = process_trace(
        "session-exception",
        store,
        MockLLM("unused"),
        events=[{"type": "start", "task": "Run still completes."}],
        config=_config(enabled=True),
    )

    assert entries == []
    assert store.entries == {}
