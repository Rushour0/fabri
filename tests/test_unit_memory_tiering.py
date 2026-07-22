"""Unit coverage for deterministic memory placement tiers."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from fabri.memory import pruning
from fabri.memory.pruning import classify_tier, ingest_guideline
from fabri.memory.schema import MemoryEntry
from fabri.orchestrator import retrieval
from fabri.orchestrator.retrieval import RetrievalConfig, retrieve_context

pytestmark = pytest.mark.unit


class MemoryStore:
    """In-memory stand-in for the narrow store interface used in these tests."""

    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self.collection = "memory-tiering-test"
        self.entries = {entry.id: entry for entry in entries or []}

    def find_by_dedup_key(
        self, dedup_key: str, kind: str | None = None
    ) -> tuple[MemoryEntry, float] | None:
        for entry in self.entries.values():
            if entry.dedup_key == dedup_key and (kind is None or entry.kind == kind):
                return entry, 1.0
        return None

    def find_similar(
        self, text: str, threshold: float = 0.85, kind: str | None = None
    ) -> tuple[MemoryEntry, float] | None:
        del text, threshold, kind
        return None

    def upsert(self, entry: MemoryEntry) -> str:
        self.entries[entry.id] = entry
        return entry.id

    def delete(self, point_id: str) -> None:
        self.entries.pop(point_id, None)

    def count(self) -> int:
        return len(self.entries)

    def iterate(self) -> list[MemoryEntry]:
        return list(self.entries.values())

    def query_by_vector(
        self,
        vector: list[float],
        top_k: int = 5,
        kind: str | None = None,
        tools_any: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        del vector
        entries = [
            entry
            for entry in self.entries.values()
            if (kind is None or entry.kind == kind)
            and (tools_any is None or set(entry.tools) & set(tools_any))
        ]
        return [(entry, 0.9 - index * 0.01) for index, entry in enumerate(entries[:top_k])]


@contextmanager
def _no_collection_lock(_collection: str):
    yield


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            MemoryEntry(
                text="This lesson was disproven.",
                kind="tactical",
                verification="contradicted",
            ),
            "quarantine",
        ),
        (
            MemoryEntry(
                text="Use the durable verification rule.",
                kind="strategic",
                verification="tool_verified",
                source_session_ids=["one", "two"],
            ),
            "core",
        ),
        (
            MemoryEntry(text="The run worked.", kind="success_pattern"),
            "quarantine",
        ),
        (
            MemoryEntry(
                text="Check the failed request payload before retrying.",
                kind="tactical",
            ),
            "retrieve",
        ),
    ],
)
def test_classify_tier_uses_deterministic_rules(entry: MemoryEntry, expected: str) -> None:
    assert classify_tier(entry) == expected


def test_merge_tier_is_raised_but_never_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pruning, "_collection_lock", _no_collection_lock)
    store = MemoryStore()
    first = ingest_guideline(
        store,
        "Keep verified deployment checks in the runbook.",
        session_id="one",
        kind="strategic",
        dedup_key="deployment-checks",
        verification="tool_verified",
        tiering_enabled=True,
    )
    assert first.tier == "retrieve"

    promoted = ingest_guideline(
        store,
        "Keep verified deployment checks in the runbook.",
        session_id="two",
        kind="strategic",
        dedup_key="deployment-checks",
        verification="tool_verified",
        tiering_enabled=True,
    )
    assert promoted.tier == "core"

    merged = ingest_guideline(
        store,
        "Keep verified deployment checks in the runbook.",
        session_id="three",
        kind="strategic",
        dedup_key="deployment-checks",
        verification="contradicted",
        tiering_enabled=True,
    )
    assert merged.verification == "contradicted"
    assert merged.tier == "core"


def test_tier_payload_round_trip_and_id_is_tier_independent() -> None:
    original = MemoryEntry(text="Preserve the deployment guard.", kind="tactical", tier="core")
    restored = MemoryEntry.from_payload(original.to_payload())
    quarantined = MemoryEntry(
        text="Preserve the deployment guard.", kind="tactical", tier="quarantine"
    )

    assert restored.tier == "core"
    assert original.id == quarantined.id


def test_retrieval_excludes_quarantine_even_when_tiering_preference_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda _text: [1.0])
    store = MemoryStore(
        [
            MemoryEntry(text="Do not inject this candidate.", kind="tactical", tier="quarantine"),
            MemoryEntry(text="Inject this ordinary lesson.", kind="tactical"),
        ]
    )

    context = retrieve_context(
        store,
        "repair the deployment",
        top_k=2,
        retrieval_config=RetrievalConfig(strategy="dense", tiering_enabled=False),
    )

    assert "Do not inject this candidate." not in context
    assert "Inject this ordinary lesson." in context


def test_tiering_preference_orders_core_before_other_injected_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda _text: [1.0])
    store = MemoryStore(
        [
            MemoryEntry(text="Higher-scored retrieve lesson.", kind="tactical", tier="retrieve"),
            MemoryEntry(text="Core lesson.", kind="strategic", tier="core"),
        ]
    )

    context = retrieve_context(
        store,
        "repair the deployment",
        top_k=2,
        retrieval_config=RetrievalConfig(strategy="dense", tiering_enabled=True),
    )

    assert context.index("Core lesson.") < context.index("Higher-scored retrieve lesson.")


def test_flag_off_keeps_ingest_and_retrieval_behavior_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pruning, "_collection_lock", _no_collection_lock)
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda _text: [1.0])
    legacy_default = MemoryStore()
    explicit_flag_off = MemoryStore()
    candidates = [
        ("Validate the error response before retrying the API request.", "tactical"),
        ("Use the documented incident checklist for the retry.", "strategic"),
    ]

    for index, (text, kind) in enumerate(candidates):
        ingest_guideline(legacy_default, text, session_id=f"session-{index}", kind=kind)
        ingest_guideline(
            explicit_flag_off,
            text,
            session_id=f"session-{index}",
            kind=kind,
            tiering_enabled=False,
        )

    legacy_entries = [(entry.id, entry.kind, entry.text, entry.tier) for entry in legacy_default.entries.values()]
    flag_off_entries = [
        (entry.id, entry.kind, entry.text, entry.tier) for entry in explicit_flag_off.entries.values()
    ]
    assert flag_off_entries == legacy_entries

    legacy_context = retrieve_context(
        legacy_default,
        "retry the API request",
        retrieval_config=RetrievalConfig(strategy="dense"),
    )
    flag_off_context = retrieve_context(
        explicit_flag_off,
        "retry the API request",
        retrieval_config=RetrievalConfig(strategy="dense", tiering_enabled=False),
    )
    assert flag_off_context == legacy_context
