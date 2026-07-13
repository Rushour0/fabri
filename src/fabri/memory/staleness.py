import time
from dataclasses import dataclass, field

from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore

# Mirrors find_stale_guidelines' own defaults; config.py's
# memory.stale_max_hit_count / memory.stale_min_age_days should match these.
DEFAULT_MAX_HIT_COUNT = 2
DEFAULT_MIN_AGE_DAYS = 7.0
DEFAULT_STALE_KINDS = ("tactical", "strategic")


@dataclass
class StaleGuideline:
    """Small read-side projection of a MemoryEntry flagged as stale (low
    hit_count relative to age). Deliberately NOT the raw MemoryEntry, so the
    CLI/JSON output contract stays stable even if MemoryEntry grows fields
    later."""

    id: str
    text: str
    kind: str
    hit_count: int
    age_days: float
    domain: str
    tags: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    created_at: float = 0.0


def _age_days(entry: MemoryEntry) -> float:
    """Same age-in-days formula pruning._eviction_score uses for eviction
    scoring -- reused as-is so "stale" and "eviction-eligible" agree on what
    "old" means."""
    return max(0.0, (time.time() - (entry.created_at or 0.0)) / 86_400.0)


def find_stale_guidelines(
    store: QdrantMemoryStore,
    *,
    max_hit_count: int = DEFAULT_MAX_HIT_COUNT,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
    kinds: tuple[str, ...] = DEFAULT_STALE_KINDS,
    limit: int | None = None,
) -> list[StaleGuideline]:
    """Report guidelines that look stale: `kind` in `kinds`, hit_count <=
    `max_hit_count`, and old enough (`age_days` >= `min_age_days`) that low
    reuse is meaningful rather than just "too new to judge". Pure read-side
    report -- does not touch scoring, retrieval, or eviction.

    Sorted oldest / least-used first (ties broken by hit_count ascending),
    then truncated to `limit` if given."""
    entries = store.iterate()  # full scan, same interface pruning.py uses

    stale = [
        entry
        for entry in entries
        if entry.kind in kinds
        and (entry.hit_count or 1) <= max_hit_count
        and _age_days(entry) >= min_age_days
    ]

    stale.sort(key=lambda e: (-_age_days(e), e.hit_count or 1))

    results = [
        StaleGuideline(
            id=entry.id,
            text=entry.text,
            kind=entry.kind,
            hit_count=entry.hit_count or 1,
            age_days=_age_days(entry),
            domain=entry.domain,
            tags=list(entry.tags or []),
            session_ids=list(entry.session_ids or []),
            created_at=entry.created_at or 0.0,
        )
        for entry in stale
    ]

    if limit is not None:
        results = results[:limit]
    return results
