import re

from fabri.memory.embeddings import embed
from fabri.memory.store import QdrantMemoryStore

DEFAULT_TOP_K = 5
# Tag-filtered hits are guaranteed inclusion in the merged list, but only if
# they're at least loosely relevant -- without a floor a stale low-score tool
# guideline crowds out genuinely relevant vector hits.
TAG_HIT_SCORE_FLOOR = 0.30


def _word_mentioned(word: str, text: str) -> bool:
    # `re.escape` keeps tool names with regex-special chars (`.`, `+`) safe.
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def retrieve_context(
    store: QdrantMemoryStore,
    task: str,
    top_k: int = DEFAULT_TOP_K,
    tool_names: list[str] | None = None,
    tag_hit_score_floor: float = TAG_HIT_SCORE_FLOOR,
) -> str:
    """Embed `task`, pull the top-k most relevant guidelines (tactical + strategic),
    and format them as a compact bullet list -- this is what gets injected into the
    agent's system prompt, so it stays just-in-time and token-cheap rather than
    dumping raw trace history.

    If `tool_names` is given, any tool whose name appears as a *whole word* in
    the task text triggers a second, tag-filtered query (guidelines tied to
    that tool via memory/pruning.py's `tools` field). Tag hits are guaranteed
    inclusion when they clear `tag_hit_score_floor` -- surfacing tool-specific
    guidelines even when their wording is too dissimilar for vector search alone
    to rank them in the top-k, but without a stale low-relevance entry crowding
    out vector hits."""
    # Word-boundary match so `read_file` doesn't trigger on every task that
    # happens to contain "read" as a substring of "already", "ready", etc.
    mentioned_tools = [t for t in (tool_names or []) if _word_mentioned(t, task)]

    # Embed once. The store accepts either a text query (re-embeds internally)
    # or a precomputed vector; passing the vector saves the per-tool re-embed
    # that the old loop paid for each mentioned tool.
    vector = embed(task)

    tag_results = []
    for tool_name in mentioned_tools:
        tag_results.extend(
            store.query_by_vector(vector, top_k=top_k, tools_any=[tool_name])
        )

    vector_results = sorted(
        store.query_by_vector(vector, top_k=top_k), key=lambda pair: pair[1], reverse=True
    )

    # Tag-filtered hits are guaranteed inclusion *if* they clear the floor.
    # Vector hits fill any remaining slots, ranked by score.
    seen_ids = set()
    merged = []
    for entry, score in tag_results:
        if score < tag_hit_score_floor:
            continue
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
