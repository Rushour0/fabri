import re
from typing import TYPE_CHECKING, Iterable

from fabri.memory.embeddings import embed
from fabri.memory.store import QdrantMemoryStore

if TYPE_CHECKING:
    from fabri.tools.manifest_schema import ToolManifest
    from fabri.tools.registry import ToolRegistry

DEFAULT_TOP_K = 5
DEFAULT_TOOL_TOP_K = 6
# Process-wide cache keyed on (tool name, description). The description rarely
# changes between runs, so an LRU-style structure isn't worth the bookkeeping;
# we just rebuild on description edits because the key changes.
_tool_embedding_cache: dict[tuple[str, str], list[float]] = {}


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _tool_vector(manifest: "ToolManifest") -> list[float]:
    key = (manifest.name, manifest.description)
    vec = _tool_embedding_cache.get(key)
    if vec is None:
        vec = embed(manifest.description or manifest.name)
        _tool_embedding_cache[key] = vec
    return vec


def retrieve_tools(
    task: str,
    registry: "ToolRegistry",
    *,
    top_k: int = DEFAULT_TOOL_TOP_K,
    always_include: Iterable[str] = (),
) -> list["ToolManifest"]:
    """Rank a registry's manifests by cosine similarity to `task` and return the
    top-K plus every name in `always_include` -- the meta-tools the
    orchestrator prompt expects (`spawn_subagent`, `ask_user`, `decompose`)
    must survive regardless of how an individual task's wording lines up.

    Embeddings are normalized by `memory.embeddings.embed`, so cosine == dot
    product. Per-tool description vectors are cached at module scope so re-runs
    over the same registry don't re-embed every tool."""
    all_tools = list(registry.list())
    if not all_tools:
        return []
    task_vec = embed(task or "")
    always_set = set(always_include)
    ranked = sorted(
        ((m, _dot(task_vec, _tool_vector(m))) for m in all_tools),
        key=lambda pair: pair[1],
        reverse=True,
    )
    selected: list["ToolManifest"] = []
    seen: set[str] = set()
    for manifest, _ in ranked:
        if len(selected) >= top_k:
            break
        selected.append(manifest)
        seen.add(manifest.name)
    for manifest in all_tools:
        if manifest.name in always_set and manifest.name not in seen:
            selected.append(manifest)
            seen.add(manifest.name)
    return selected

# Tag-filtered hits are guaranteed inclusion in the merged list, but only if
# they're at least loosely relevant -- without a floor a stale low-score tool
# guideline crowds out genuinely relevant vector hits.
TAG_HIT_SCORE_FLOOR = 0.30


# Compiled word-boundary patterns are cached per tool name. `retrieve_context`
# is hit on every run, and rebuilding `\bname\b` for every tool on every call
# is pure waste once the registry is stable.
_word_pattern_cache: dict[str, re.Pattern[str]] = {}


def _word_mentioned(word: str, text: str) -> bool:
    pattern = _word_pattern_cache.get(word)
    if pattern is None:
        # `re.escape` keeps tool names with regex-special chars (`.`, `+`) safe.
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        _word_pattern_cache[word] = pattern
    return pattern.search(text) is not None


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
    # Cold store: nothing to retrieve against, so skip the (expensive) embed
    # call entirely. This means a fresh `fabri init` + first `fabri run` never
    # has to load the 44MB sentence-transformers model.
    if store.count() == 0:
        return ""

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

    # A4: success_pattern entries get a guaranteed share of the top-k slots so
    # a flood of failure-derived guidelines can't drown them at retrieval
    # time. We over-fetch by 2x and then take up to top_k//2 success patterns
    # before filling the rest with the highest-ranked entries of any kind.
    success_results = sorted(
        [pair for pair in store.query_by_vector(vector, top_k=top_k * 2)
         if pair[0].kind == "success_pattern"],
        key=lambda pair: pair[1], reverse=True,
    )
    success_cap = max(1, top_k // 2) if success_results else 0

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
    # Reserved success-pattern slots (A4): up to top_k//2.
    success_added = 0
    for entry, score in success_results:
        if success_added >= success_cap or len(merged) >= top_k:
            break
        if entry.id not in seen_ids:
            seen_ids.add(entry.id)
            merged.append((entry, score))
            success_added += 1
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
