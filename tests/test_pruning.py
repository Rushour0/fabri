import uuid

from agent_memory.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, ingest_guideline
from agent_memory.memory.store import QdrantMemoryStore

COLLECTION = f"test_{uuid.uuid4().hex[:8]}"


def make_store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=COLLECTION)


def test_duplicate_guideline_increments_hit_count_not_inserted_twice():
    store = make_store()
    text = "Do not trust the flaky tool's output."

    e1 = ingest_guideline(store, text, session_id="s1")
    e2 = ingest_guideline(store, text, session_id="s2")

    assert e1.id == e2.id
    assert e2.hit_count == 2
    assert set(e2.session_ids) == {"s1", "s2"}
    assert store.count() == 1

    store.delete(e2.id)


def test_promotion_to_strategic_after_threshold_sessions():
    store = make_store()
    text = "Recurring failure that should eventually get promoted."

    entry = None
    for i in range(PROMOTION_THRESHOLD_SESSIONS):
        entry = ingest_guideline(store, text, session_id=f"session-{i}")

    assert entry.kind == "strategic"
    assert len(set(entry.session_ids)) == PROMOTION_THRESHOLD_SESSIONS

    store.delete(entry.id)


def test_distinct_guidelines_are_not_merged():
    store = make_store()
    e1 = ingest_guideline(store, "Guideline about tool A.", session_id="s1")
    e2 = ingest_guideline(store, "Completely unrelated guideline about map rendering.", session_id="s1")

    assert e1.id != e2.id
    assert store.count() == 2

    store.delete(e1.id)
    store.delete(e2.id)


def test_tools_accumulate_across_merges_not_overwritten():
    store = make_store()
    text = "Recurring guideline tied to more than one tool."

    e1 = ingest_guideline(store, text, session_id="s1", tools=["broken"])
    e2 = ingest_guideline(store, text, session_id="s2", tools=["sum"])

    assert e1.id == e2.id
    assert set(e2.tools) == {"broken", "sum"}

    store.delete(e2.id)
