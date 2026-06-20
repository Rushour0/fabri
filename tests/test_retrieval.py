import uuid

from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.retrieval import retrieve_context

COLLECTION = f"test_{uuid.uuid4().hex[:8]}"


def make_store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=COLLECTION)


def test_tools_any_filter_only_returns_tagged_entries():
    store = make_store()
    tagged = MemoryEntry(text="Guideline tagged to the sum tool.", kind="tactical", tools=["sum"])
    untagged = MemoryEntry(text="Some unrelated guideline.", kind="tactical")
    store.upsert(tagged)
    store.upsert(untagged)

    results = store.query("irrelevant query text", top_k=5, tools_any=["sum"])
    assert [e.text for e, _ in results] == [tagged.text]

    store.delete(tagged.id)
    store.delete(untagged.id)


def test_tag_filtered_guideline_surfaces_despite_low_vector_similarity():
    store = make_store()
    # Deliberately lexically dissimilar from the query, but tagged to "broken".
    low_similarity_guideline = MemoryEntry(
        text="Xyzzy plugh quux frobnicate.", kind="tactical", tools=["broken"]
    )
    # Five unrelated but more "normal" entries that vector search would rank ahead of it.
    fillers = [MemoryEntry(text=f"Some generic unrelated guideline number {i}.", kind="tactical") for i in range(5)]
    store.upsert(low_similarity_guideline)
    for f in fillers:
        store.upsert(f)

    context = retrieve_context(store, "please use the broken tool now", top_k=5, tool_names=["broken"])
    assert "Xyzzy plugh quux frobnicate." in context

    store.delete(low_similarity_guideline.id)
    for f in fillers:
        store.delete(f.id)


def test_no_tool_mentioned_falls_back_to_vector_only():
    store = make_store()
    entry = MemoryEntry(text="Prefer the sum tool for addition.", kind="tactical", tools=["sum"])
    store.upsert(entry)

    context = retrieve_context(store, "how do I add two numbers", top_k=5, tool_names=["sum"])
    assert "Prefer the sum tool for addition." in context

    store.delete(entry.id)
