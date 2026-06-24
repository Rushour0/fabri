import threading
import uuid

from fabri.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, ingest_guideline
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore

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


def test_recurrence_of_promoted_guideline_does_not_demote_or_duplicate():
    store = make_store()
    text = "Promoted lesson that recurs again after going strategic."

    entry = None
    for i in range(PROMOTION_THRESHOLD_SESSIONS):
        entry = ingest_guideline(store, text, session_id=f"s{i}")
    assert entry.kind == "strategic"

    # One more recurrence (new session) of the SAME lesson: must merge into the
    # existing strategic entry, not insert a fresh tactical dup or clobber it
    # back to tactical.
    again = ingest_guideline(store, text, session_id="s-later")
    assert again.id == entry.id
    assert again.kind == "strategic"
    assert store.count() == 1
    assert store.count(kind="tactical") == 0

    store.delete(entry.id)


def test_distinct_guidelines_are_not_merged():
    store = make_store()
    e1 = ingest_guideline(store, "Guideline about tool A.", session_id="s1")
    e2 = ingest_guideline(store, "Completely unrelated guideline about map rendering.", session_id="s1")

    assert e1.id != e2.id
    assert store.count() == 2

    store.delete(e1.id)
    store.delete(e2.id)


def test_success_pattern_does_not_merge_into_textually_similar_tactical():
    # A4: success and failure-derived guidelines stay separated in dedup so a
    # success_pattern can't suppress a failure guideline (or vice versa) just
    # because the synthesized text happens to be close.
    store = make_store()
    text = "Use spawn_subagent to fan out work across map and character domains."

    failure_entry = ingest_guideline(store, text, session_id="s1", kind="tactical")
    success_entry = ingest_guideline(store, text, session_id="s2", kind="success_pattern")

    assert failure_entry.id != success_entry.id
    assert failure_entry.kind == "tactical"
    assert success_entry.kind == "success_pattern"
    assert store.count() == 2

    store.delete(failure_entry.id)
    store.delete(success_entry.id)


def test_tools_accumulate_across_merges_not_overwritten():
    store = make_store()
    text = "Recurring guideline tied to more than one tool."

    e1 = ingest_guideline(store, text, session_id="s1", tools=["broken"])
    e2 = ingest_guideline(store, text, session_id="s2", tools=["sum"])

    assert e1.id == e2.id
    assert set(e2.tools) == {"broken", "sum"}

    store.delete(e2.id)


def test_concurrent_ingest_does_not_lose_updates():
    """Two ingests of the SAME guideline racing on one collection must BOTH be
    counted: the per-collection flock serializes find_similar->update->upsert so
    the final hit_count equals the number of ingests (regression for the
    lost-update race the flock was added to fix)."""
    text = "Concurrent lesson that must survive a race without a lost update."
    n = 8
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def _worker(i: int) -> None:
        try:
            # Each thread gets its own store/client + its own lock-file fd, so
            # the flock genuinely arbitrates across them (not a single in-proc fd).
            barrier.wait(timeout=10)
            ingest_guideline(QdrantMemoryStore(collection=COLLECTION), text, session_id=f"c{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    store = make_store()
    entry_id = MemoryEntry(text=text, kind="tactical").id  # deterministic id
    merged = store.get(entry_id)
    assert merged is not None
    assert merged.hit_count == n  # no update was dropped
    assert len(set(merged.session_ids)) == n
    assert store.count() == 1
    store.delete(entry_id)
