import fcntl
import re
from contextlib import contextmanager

from fabri.core.logging_setup import get_logger
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore
from fabri.paths import locks_dir

SIMILARITY_THRESHOLD = 0.85
PROMOTION_THRESHOLD_SESSIONS = 3

logger = get_logger()


@contextmanager
def _collection_lock(collection: str):
    """Serialize the find_similar -> update -> upsert critical section across
    concurrent processes ingesting into the same Qdrant collection (e.g. a
    parent agent and a sub-agent sharing one memory store). Without this, both
    readers see hit_count=N, both write N+1, and one merge is lost.

    Per-collection flock on a file under .fabri/locks/. The lock is released
    when the fd is closed; held for the duration of one ingest_guideline call,
    which is two Qdrant round-trips, not a long-running operation."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", collection)
    path = locks_dir() / f"{safe}.ingest.lock"
    f = path.open("a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def ingest_guideline(
    store: QdrantMemoryStore,
    text: str,
    session_id: str,
    tags: list[str] | None = None,
    tools: list[str] | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    promotion_threshold_sessions: int = PROMOTION_THRESHOLD_SESSIONS,
    kind: str = "tactical",
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
    with _collection_lock(store.collection):
        # A4: success_pattern entries are deduped against same-kind entries
        # only, so a "what worked" guideline doesn't merge into a textually
        # similar failure-derived guideline (or vice versa) and silently
        # suppress one of the two signals at retrieval time. Failure-derived
        # kinds (tactical / strategic) still search across both -- that's the
        # promotion path the historical pruning already relied on.
        if kind == "success_pattern":
            existing = store.find_similar(text, threshold=similarity_threshold, kind="success_pattern")
        else:
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

        entry = MemoryEntry(text=text, kind=kind, session_ids=[session_id], tags=tags, tools=tools)
        logger.debug("inserted new %s guideline: %r tools=%s", kind, entry.text, entry.tools)
        store.upsert(entry)
        return entry
