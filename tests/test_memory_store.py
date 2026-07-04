import time
import uuid

import pytest

from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore

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


# ---------------------------------------------------------------------------
# Schema backward-compat: new fields default gracefully from old payloads
# ---------------------------------------------------------------------------

def test_schema_new_fields_default_on_old_payload():
    """Old payloads stored before the schema extension must round-trip safely."""
    old_payload = {
        "text": "use the correct tool",
        "kind": "tactical",
        "session_ids": ["s1"],
        "tags": [],
        "tools": [],
        "hit_count": 1,
        "created_at": time.time(),
        "model_version": "minilm-l6-v2",
        # No domain, outcome, agent_id, task_embedding_hash
    }
    entry = MemoryEntry.from_payload(old_payload)
    assert entry.domain == "generic"
    assert entry.outcome == "unknown"
    assert entry.agent_id is None
    assert entry.task_embedding_hash is None


def test_schema_new_fields_round_trip():
    """New fields survive to_payload → from_payload."""
    entry = MemoryEntry(
        text="call the api endpoint correctly",
        kind="success_pattern",
        domain="api",
        outcome="success",
        agent_id="agent-42",
        task_embedding_hash="deadbeef",
    )
    payload = entry.to_payload()
    restored = MemoryEntry.from_payload(payload)
    assert restored.domain == "api"
    assert restored.outcome == "success"
    assert restored.agent_id == "agent-42"
    assert restored.task_embedding_hash == "deadbeef"


def test_schema_id_unchanged_by_new_fields():
    """Adding new optional fields must not change the deterministic ID."""
    base = MemoryEntry(text="read the file carefully", kind="tactical")
    extended = MemoryEntry(
        text="read the file carefully",
        kind="tactical",
        domain="code",
        outcome="failure",
        agent_id="some-agent",
        task_embedding_hash="abc12345",
    )
    assert base.id == extended.id
