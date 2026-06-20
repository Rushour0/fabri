import uuid

from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.retrieval import retrieve_context, retrieve_tools
from fabri.tools.manifest_schema import ToolManifest

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


class _FakeRegistry:
    """Minimal stand-in for ToolRegistry that retrieve_tools needs -- just
    .list(). Lets us assemble a synthetic manifest set without touching the
    filesystem."""
    def __init__(self, manifests: list[ToolManifest]):
        self._manifests = manifests

    def list(self) -> list[ToolManifest]:
        return list(self._manifests)


def _manifest(name: str, description: str) -> ToolManifest:
    return ToolManifest(
        name=name, description=description, command=["true"],
        input_schema={"type": "object"}, output_schema={"type": "object"},
    )


def test_retrieve_tools_ranks_relevant_first_and_caps_top_k():
    registry = _FakeRegistry([
        _manifest("read_file", "Read the contents of a file from the sandbox."),
        _manifest("write_file", "Write content to a file in the sandbox."),
        _manifest("web_search", "Search the public web for a query string."),
        _manifest("weather_lookup", "Look up the current weather in a city."),
        _manifest("currency_convert", "Convert an amount between two currencies."),
    ])
    selected = retrieve_tools(
        "I need to read a file from disk", registry, top_k=2, always_include=()
    )
    names = [m.name for m in selected]
    assert len(selected) == 2
    # read_file should rank #1 for the read-a-file query.
    assert names[0] == "read_file"


def test_retrieve_tools_always_include_survives_low_similarity():
    registry = _FakeRegistry([
        _manifest("read_file", "Read the contents of a file from the sandbox."),
        _manifest("web_search", "Search the public web for a query string."),
        _manifest("decompose", "Break a task into sub-questions."),
        _manifest("ask_user", "Pause and ask a human clarifying question."),
        _manifest("spawn_subagent", "Spawn a child agent to handle a subtask."),
    ])
    selected = retrieve_tools(
        "fetch the latest weather report from a webpage",
        registry,
        top_k=1,
        always_include=("spawn_subagent", "ask_user", "decompose"),
    )
    names = {m.name for m in selected}
    # top-1 vector hit + 3 always-include = 4 entries total.
    assert {"spawn_subagent", "ask_user", "decompose"}.issubset(names)
    assert len(selected) == 4


def test_retrieve_tools_empty_registry_is_empty():
    assert retrieve_tools("anything", _FakeRegistry([])) == []


def test_success_pattern_gets_reserved_slot_in_blend():
    # A4: success_pattern entries get up to top_k//2 reserved slots so a flood
    # of failure-derived guidelines can't drown them at retrieval time.
    store = make_store()
    success = MemoryEntry(
        text="What worked: spawned map + character subagents in parallel.",
        kind="success_pattern",
    )
    fillers = [
        MemoryEntry(text=f"Failure-derived guideline number {i}.", kind="tactical")
        for i in range(6)
    ]
    store.upsert(success)
    for f in fillers:
        store.upsert(f)

    context = retrieve_context(store, "spawn map and character subagents", top_k=4)
    assert "[success_pattern]" in context

    store.delete(success.id)
    for f in fillers:
        store.delete(f.id)


def test_no_tool_mentioned_falls_back_to_vector_only():
    store = make_store()
    entry = MemoryEntry(text="Prefer the sum tool for addition.", kind="tactical", tools=["sum"])
    store.upsert(entry)

    context = retrieve_context(store, "how do I add two numbers", top_k=5, tool_names=["sum"])
    assert "Prefer the sum tool for addition." in context

    store.delete(entry.id)
