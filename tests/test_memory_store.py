import uuid

from agent_memory.memory.schema import MemoryEntry
from agent_memory.memory.store import QdrantMemoryStore

COLLECTION = f"test_{uuid.uuid4().hex[:8]}"


def make_store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=COLLECTION)


def test_upsert_and_get_round_trip():
    store = make_store()
    entry = MemoryEntry(text="Test guideline one.", kind="tactical", session_ids=["s1"])
    point_id = store.upsert(entry)

    fetched = store.get(point_id)
    assert fetched is not None
    assert fetched.text == entry.text
    assert fetched.kind == "tactical"
    assert fetched.session_ids == ["s1"]

    store.delete(point_id)


def test_idempotent_upsert_same_text():
    store = make_store()
    e1 = MemoryEntry(text="Idempotency check guideline.", kind="tactical", session_ids=["s1"])
    e2 = MemoryEntry(text="Idempotency check guideline.", kind="tactical", session_ids=["s2"])

    id1 = store.upsert(e1)
    id2 = store.upsert(e2)
    assert id1 == id2

    store.delete(id1)


def test_query_ranks_relevant_entry_first():
    store = make_store()
    relevant = MemoryEntry(text="Prefer the sum tool for numeric addition.", kind="tactical")
    unrelated = MemoryEntry(text="Forge interior maps use a different VRAM screenblock.", kind="tactical")
    store.upsert(relevant)
    store.upsert(unrelated)

    results = store.query("how do I add two numbers?", top_k=2)
    assert results[0][0].text == relevant.text

    store.delete(relevant.id)
    store.delete(unrelated.id)


def test_find_similar_matches_paraphrase():
    store = make_store()
    entry = MemoryEntry(text="Always confirm destructive operations before running them.", kind="tactical")
    store.upsert(entry)

    match = store.find_similar("Always confirm destructive ops before running.", threshold=0.6)
    assert match is not None

    store.delete(entry.id)
