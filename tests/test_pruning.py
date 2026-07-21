import threading
import uuid

from fabri.memory.pruning import PROMOTION_THRESHOLD_SESSIONS, ingest_guideline
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.pipeline import guideline_dedup_key

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


def test_dedup_key_merges_paraphrases_but_different_tasks_do_not_merge():
    store = make_store()
    first_key = guideline_dedup_key(
        "tactical", task="Repair the billing import", failed_tool_name="import_csv", error_text="Missing account id",
    )
    second_key = guideline_dedup_key(
        "tactical", task="Repair the inventory import", failed_tool_name="import_csv", error_text="Missing account id",
    )
    first = ingest_guideline(
        store, "Validate account identifiers before loading the billing file.", session_id="s1", dedup_key=first_key,
    )
    merged = ingest_guideline(
        store, "Check every customer reference before retrying the financial upload.", session_id="s2", dedup_key=first_key,
    )
    distinct = ingest_guideline(
        store, "Validate account identifiers before loading the inventory file.", session_id="s3", dedup_key=second_key,
    )

    # The second paraphrase is longer/more informative than the first, so the
    # merge upgrades the stored text -- and since entry.id is a hash of the
    # text (see MemoryEntry.id), the merged entry's id necessarily changes
    # along with it. What must hold is that this stays a single point (no
    # orphaned stale point left behind) with counters carried over.
    assert merged.id != first.id
    assert merged.text == "Check every customer reference before retrying the financial upload."
    assert merged.hit_count == 2
    assert set(merged.session_ids) == {"s1", "s2"}
    assert distinct.id != merged.id
    assert store.count() == 2

    store.delete(merged.id)
    store.delete(distinct.id)


def test_merge_upgrades_text_to_the_longer_more_informative_candidate():
    store = make_store()
    key = guideline_dedup_key(
        "tactical", task="Repair the billing import", failed_tool_name="import_csv", error_text="Missing account id",
    )
    short_text = "Validate account ids first."
    longer_text = "Validate account identifiers against the customer table before loading the billing file, since missing ids abort the whole batch."

    first = ingest_guideline(store, short_text, session_id="s1", dedup_key=key)
    assert first.text == short_text

    # entry.id is a hash of entry.text (see MemoryEntry.id), so upgrading the
    # text necessarily changes the id too -- what matters is that the merge
    # still collapses to a single stored point (no orphan of the old, less
    # informative point) with counters/session_ids carried over.
    upgraded = ingest_guideline(store, longer_text, session_id="s2", dedup_key=key)
    assert upgraded.id != first.id
    assert upgraded.text == longer_text
    assert upgraded.hit_count == 2
    assert set(upgraded.session_ids) == {"s1", "s2"}
    assert store.count() == 1

    # A subsequent shorter/less-informative recurrence must NOT downgrade the
    # already-upgraded text, and since the text doesn't change this time, the
    # id stays stable.
    not_downgraded = ingest_guideline(store, short_text, session_id="s3", dedup_key=key)
    assert not_downgraded.id == upgraded.id
    assert not_downgraded.text == longer_text
    assert not_downgraded.hit_count == 3
    assert store.count() == 1

    store.delete(not_downgraded.id)


def test_concurrent_ingest_does_not_lose_updates():
    """Two ingests of the SAME guideline racing on one collection must BOTH be
    counted: the per-collection flock serializes find_similar->update->upsert so
    the final hit_count equals the number of ingests (regression for the
    lost-update race the flock was added to fix)."""
    text = "Concurrent lesson that must survive a race without a lost update."
    n = 8

    # Create and warm the shared collection before the threads race. This test
    # exercises the flock's lost-update prevention, not collection creation:
    # if the 8 workers below each raced to create the collection on first store
    # construction and then queried it before Qdrant finished initializing its
    # segments, that surfaced as a transient 500 "0 of 0 read operations failed"
    # (and a create-conflict 409) under load -- flakiness unrelated to what the
    # flock guarantees. A warm-up query forces the collection ready first, so
    # the workers only exercise the serialized find->update->upsert path,
    # exactly as production does against an already-established collection.
    QdrantMemoryStore(collection=COLLECTION).find_similar(text)

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
