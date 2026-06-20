from fabri.memory.store import QdrantMemoryStore

DEFAULT_TOP_K = 5


def retrieve_context(
    store: QdrantMemoryStore, task: str, top_k: int = DEFAULT_TOP_K, tool_names: list[str] | None = None
) -> str:
    """Embed `task`, pull the top-k most relevant guidelines (tactical + strategic),
    and format them as a compact bullet list -- this is what gets injected into the
    agent's system prompt, so it stays just-in-time and token-cheap rather than
    dumping raw trace history.

    If `tool_names` is given, any tool name that appears as a substring of the
    task text also triggers a second, tag-filtered query (guidelines tied to that
    tool via memory/pruning.py's `tools` field) -- this surfaces tool-specific
    guidelines even when their wording is too dissimilar for vector search alone
    to rank them in the top-k."""
    mentioned_tools = [t for t in (tool_names or []) if t.lower() in task.lower()]
    tag_results = []
    for tool_name in mentioned_tools:
        tag_results.extend(store.query(task, top_k=top_k, tools_any=[tool_name]))

    vector_results = sorted(store.query(task, top_k=top_k), key=lambda pair: pair[1], reverse=True)

    # Tag-filtered hits are guaranteed inclusion -- the whole point is to surface
    # them even when their vector score is too low to make the top-k on its own.
    # Vector hits fill any remaining slots, ranked by score.
    seen_ids = set()
    merged = []
    for entry, score in tag_results:
        if entry.id not in seen_ids:
            seen_ids.add(entry.id)
            merged.append((entry, score))
    for entry, score in vector_results:
        if len(merged) >= top_k:
            break
        if entry.id not in seen_ids:
            seen_ids.add(entry.id)
            merged.append((entry, score))

    if not merged:
        return ""
    lines = [f"- [{entry.kind}] {entry.text}" for entry, _score in merged]
    return "Relevant guidelines from past sessions:\n" + "\n".join(lines)
