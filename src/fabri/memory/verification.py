"""Apply external evidence verdicts to mined memory entries."""
from __future__ import annotations

from typing import Protocol

from fabri.memory.schema import MemoryEntry

_VERDICTS = frozenset({"tool_verified", "rubric_verified", "contradicted"})
_VERIFIED_VALUES = frozenset(
    {"tool_verified", "rubric_verified", "human_verified", "config_verified"}
)


class MutableMemoryStore(Protocol):
    def iterate(self, kind: str | None = None, limit: int | None = None) -> list[MemoryEntry]: ...

    def upsert(self, entry: MemoryEntry) -> str: ...


def verification_allowed(entry: MemoryEntry) -> bool:
    """Return whether an entry carries an affirmative verification value."""
    return entry.verification in _VERIFIED_VALUES


def apply_session_verification(
    store: MutableMemoryStore,
    source_session_id: str,
    verdict: str,
    *,
    kinds: frozenset[str] = frozenset({"success_pattern"}),
) -> list[str]:
    """Update entries sourced from one externally scored session."""
    if verdict not in _VERDICTS:
        raise ValueError(f"unsupported memory verification verdict: {verdict}")
    updated: list[str] = []
    for entry in store.iterate():
        if entry.kind not in kinds or source_session_id not in entry.source_session_ids:
            continue
        entry.verification = verdict
        store.upsert(entry)
        updated.append(entry.id)
    return sorted(updated)
