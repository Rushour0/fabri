from fabri.core.logging_setup import get_logger
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore

SIMILARITY_THRESHOLD = 0.85
PROMOTION_THRESHOLD_SESSIONS = 3

logger = get_logger()


def ingest_guideline(
    store: QdrantMemoryStore,
    text: str,
    session_id: str,
    tags: list[str] | None = None,
    tools: list[str] | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    promotion_threshold_sessions: int = PROMOTION_THRESHOLD_SESSIONS,
) -> MemoryEntry:
    """Insert or merge a new candidate guideline. A near-duplicate of an existing
    entry (cosine sim >= similarity_threshold, tactical *or* strategic) increments
    that entry's recurrence count instead of inserting a duplicate; once it has
    recurred across >= promotion_threshold_sessions distinct sessions, it's
    promoted to strategic. A merge never demotes: an already-strategic entry stays
    strategic. `tools` accumulates (union) across merges rather than overwriting,
    since the same guideline can end up associated with more than one tool over
    time."""
    tags = tags or []
    tools = tools or []
    # Search across both kinds: matching only tactical entries would let a
    # recurrence of a promoted guideline slip through as a brand-new tactical
    # dup (restarting its promotion counter), or -- when the synthesized text is
    # identical -- collide on the deterministic point id and clobber the
    # strategic entry back down to tactical on upsert.
    existing = store.find_similar(text, threshold=similarity_threshold, kind=None)

    if existing is not None:
        entry, score = existing
        if session_id not in entry.session_ids:
            entry.session_ids.append(session_id)
        entry.hit_count += 1
        for tool_name in tools:
            if tool_name not in entry.tools:
                entry.tools.append(tool_name)
        promoted = len(set(entry.session_ids)) >= promotion_threshold_sessions
        if promoted and entry.kind != "strategic":
            logger.info("promoting guideline to strategic (sim=%.2f): %r", score, entry.text)
            entry.kind = "strategic"
        else:
            logger.debug("merged duplicate guideline (sim=%.2f, hit_count=%d): %r", score, entry.hit_count, entry.text)
        store.upsert(entry)
        return entry

    entry = MemoryEntry(text=text, kind="tactical", session_ids=[session_id], tags=tags, tools=tools)
    logger.debug("inserted new tactical guideline: %r tools=%s", entry.text, entry.tools)
    store.upsert(entry)
    return entry


def evict_stale(store: QdrantMemoryStore, min_hit_count: int = 1) -> int:
    """Remove strategic entries that never proved useful. Intended to run
    periodically (e.g. a maintenance cron), not on every ingest."""
    removed = 0
    offset = None
    while True:
        points, offset = store.client.scroll(
            collection_name=store.collection, limit=100, offset=offset
        )
        for p in points:
            entry = MemoryEntry.from_payload(p.payload)
            if entry.kind == "strategic" and entry.hit_count < min_hit_count:
                store.delete(entry.id)
                removed += 1
        if offset is None:
            break
    return removed
