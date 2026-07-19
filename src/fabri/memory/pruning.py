import fcntl
import math
import re
import time
from contextlib import contextmanager
from typing import Callable

from fabri.core.logging_setup import get_logger
from fabri.memory.compress import DEFAULT_MAX_TOKENS, enforce_token_cap
from fabri.memory.schema import MemoryEntry
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.retrieval import _classify_domain
from fabri.paths import locks_dir

SIMILARITY_THRESHOLD = 0.85
PROMOTION_THRESHOLD_SESSIONS = 3

logger = get_logger()

# Kinds that are protected from eviction until no other entries remain.
_EVICTION_PROTECTED_KINDS = frozenset({"strategic"})


def _eviction_score(entry: MemoryEntry, half_life_days: float) -> float:
    """Time-weighted retention score. Higher = keep; lower = evict first.

    Combines hit_count (how useful the entry has been) with an exponential
    temporal decay so old, rarely-used entries are evicted before recent or
    frequently-retrieved ones. Strategic entries are handled by the caller
    (they are protected until all non-protected entries are gone).

      score = hit_count * exp(-ln(2) * age_days / half_life_days)

    At age=0  → score = hit_count  (full weight)
    At age=half_life_days → score = hit_count / 2
    At age=3*half_life_days → score ≈ hit_count / 8
    """
    age_days = max(0.0, (time.time() - (entry.created_at or 0.0)) / 86_400.0)
    decay = math.exp(-math.log(2) * age_days / max(half_life_days, 1e-9))
    return max(entry.hit_count or 1, 1) * decay


_SUMMARY_CHUNK = 5


def _summarize_and_evict(
    store: QdrantMemoryStore,
    to_delete: list[MemoryEntry],
    llm,
    guideline_max_tokens: int,
    on_usage: Callable | None,
) -> None:
    """MemGPT-style eviction: compress groups of low-scoring entries into a
    single summarized guideline before deleting the originals.

    Strategy (Generative Agents-inspired hierarchical compression):
      - Non-protected entries (tactical, success_pattern, postmortem) are
        chunked sequentially in groups of _SUMMARY_CHUNK. Each group is
        summarized by the LLM into one compressed tactical guideline, then
        the originals are deleted. Sequential chunking avoids an embedding
        call at eviction time while still capturing the shared pattern.
      - Strategic entries are summarized individually to avoid merging
        entries from different domains/policies. The compressed entry keeps
        the strategic kind so it remains protected in future evictions.

    Falls back to plain delete for any group if the LLM call fails.

    Note: each group of N is replaced by 1, so the net reduction per chunk
    is N-1. For small `need_to_remove` this may leave the store 1-2 entries
    over limit until the next ingest triggers a follow-up eviction pass."""
    non_protected = [e for e in to_delete if e.kind not in _EVICTION_PROTECTED_KINDS]
    strategics = [e for e in to_delete if e.kind in _EVICTION_PROTECTED_KINDS]

    for i in range(0, len(non_protected), _SUMMARY_CHUNK):
        group = non_protected[i : i + _SUMMARY_CHUNK]
        combined = "\n".join(f"- {e.text}" for e in group)
        prompt = (
            f"Compress these {len(group)} agent guidelines into ONE concise "
            f"actionable guideline (max {guideline_max_tokens} tokens) that "
            f"captures the key shared pattern:\n{combined}"
        )
        try:
            response = llm.step(
                "You compress agent guidelines into one concise generalized rule.",
                [{"role": "user", "content": prompt}],
            )
            if on_usage is not None and response.usage is not None:
                on_usage(response.usage)
            summary_text = enforce_token_cap(
                (response.final_text or combined).strip(), guideline_max_tokens
            )
        except Exception as exc:
            logger.warning(
                "eviction summarization failed for group of %d (%s); falling back to delete",
                len(group), exc,
            )
            for e in group:
                store.delete(e.id)
            continue

        merged_tools = list(dict.fromkeys(t for e in group for t in (e.tools or [])))
        merged_sessions = list(dict.fromkeys(s for e in group for s in (e.session_ids or [])))
        summary_entry = MemoryEntry(
            text=summary_text,
            kind="tactical",
            session_ids=merged_sessions,
            tools=merged_tools,
            hit_count=max((e.hit_count or 1) for e in group),
            domain=_classify_domain(summary_text, merged_tools),
            outcome="unknown",
        )
        store.upsert(summary_entry)
        logger.info(
            "compressed %d evicted entries into tactical guideline: %r",
            len(group), summary_text[:60],
        )
        for e in group:
            store.delete(e.id)

    # Strategic entries are summarized individually to preserve domain boundaries.
    for entry in strategics:
        prompt = (
            f"Compress this agent guideline into ONE more concise version "
            f"(max {guideline_max_tokens} tokens):\n{entry.text}"
        )
        try:
            response = llm.step(
                "You compress agent guidelines into one concise generalized rule.",
                [{"role": "user", "content": prompt}],
            )
            if on_usage is not None and response.usage is not None:
                on_usage(response.usage)
            summary_text = enforce_token_cap(
                (response.final_text or entry.text).strip(), guideline_max_tokens
            )
        except Exception as exc:
            logger.warning(
                "strategic eviction summarization failed (%s); falling back to delete", exc
            )
            store.delete(entry.id)
            continue

        summary_entry = MemoryEntry(
            text=summary_text,
            kind="strategic",  # compressed strategic entry stays strategic
            session_ids=list(entry.session_ids or []),
            tools=list(entry.tools or []),
            hit_count=entry.hit_count or 1,
            domain=entry.domain,
            outcome=entry.outcome or "unknown",
        )
        store.upsert(summary_entry)
        logger.info("compressed strategic guideline into: %r", summary_text[:60])
        store.delete(entry.id)


def _evict_if_needed(
    store: QdrantMemoryStore,
    max_entries: int,
    half_life_days: float = 30.0,
    strategy: str = "delete",
    llm=None,
    guideline_max_tokens: int = DEFAULT_MAX_TOKENS,
    on_usage: Callable | None = None,
) -> int:
    """Evict the lowest-scoring entries until `store.count() <= max_entries`.

    Eviction order:
      1. Non-protected kinds (tactical, success_pattern, postmortem) sorted by
         _eviction_score ascending (lowest score = evict first).
      2. Protected kinds (strategic) only if the collection is still over limit.

    When `strategy="summarize"` and `llm` is provided, groups of evicted
    entries are compressed into a single guideline before deletion (MemGPT-
    style), preserving the information signal. Falls back to plain delete
    when `llm` is None or when a summarization call fails.

    Returns the number of entries processed for eviction. Called by
    `ingest_guideline` when `max_entries` is set, outside the per-ingest
    flock so it doesn't hold the lock during a potentially slow iterate()."""
    total = store.count()
    if total <= max_entries:
        return 0

    entries = store.iterate()  # full scan — bounded when max_entries is set
    need_to_remove = total - max_entries

    non_protected = [e for e in entries if e.kind not in _EVICTION_PROTECTED_KINDS]
    protected = [e for e in entries if e.kind in _EVICTION_PROTECTED_KINDS]

    # Sort ascending: lowest score = evict first.
    non_protected.sort(key=lambda e: _eviction_score(e, half_life_days))
    protected.sort(key=lambda e: _eviction_score(e, half_life_days))

    candidates = non_protected + protected
    to_delete = candidates[:need_to_remove]

    if strategy == "summarize" and llm is not None and to_delete:
        _summarize_and_evict(store, to_delete, llm, guideline_max_tokens, on_usage)
    else:
        for entry in to_delete:
            logger.info(
                "evicting %s guideline (score=%.3f, hit_count=%d, age_days=%.1f): %r",
                entry.kind,
                _eviction_score(entry, half_life_days),
                entry.hit_count or 1,
                max(0.0, (time.time() - (entry.created_at or 0.0)) / 86_400.0),
                entry.text[:60],
            )
            store.delete(entry.id)

    return len(to_delete)


@contextmanager
def _collection_lock(collection: str):
    """Serialize the find_similar -> update -> upsert critical section across
    concurrent processes ingesting into the same Qdrant collection (e.g. a
    parent agent and a sub-agent sharing one memory store). Without this, both
    readers see hit_count=N, both write N+1, and one merge is lost.

    Per-collection flock on a file under .fabri/locks/. The lock is released
    when the fd is closed; held for the duration of one ingest_guideline call,
    which is two Qdrant round-trips, not a long-running operation."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", collection)
    path = locks_dir() / f"{safe}.ingest.lock"
    f = path.open("a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def ingest_guideline(
    store: QdrantMemoryStore,
    text: str,
    session_id: str,
    tags: list[str] | None = None,
    tools: list[str] | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    promotion_threshold_sessions: int = PROMOTION_THRESHOLD_SESSIONS,
    kind: str = "tactical",
    dedup_key: str | None = None,
    max_entries: int | None = None,
    eviction_half_life_days: float = 30.0,
    eviction_strategy: str = "delete",
    eviction_llm=None,
    eviction_guideline_max_tokens: int = DEFAULT_MAX_TOKENS,
    on_eviction_usage: Callable | None = None,
) -> MemoryEntry:
    """Insert or merge a new candidate guideline. A near-duplicate of an existing
    entry (cosine sim >= similarity_threshold, tactical *or* strategic) increments
    that entry's recurrence count instead of inserting a duplicate; once it has
    recurred across >= promotion_threshold_sessions distinct sessions, it's
    promoted to strategic. A merge never demotes: an already-strategic entry stays
    strategic. `tools` accumulates (union) across merges rather than overwriting,
    since the same guideline can end up associated with more than one tool over
    time."""
    tags = tags or []
    tools = tools or []
    with _collection_lock(store.collection):
        # A4: success_pattern entries are deduped against same-kind entries
        # only, so a "what worked" guideline doesn't merge into a textually
        # similar failure-derived guideline (or vice versa) and silently
        # suppress one of the two signals at retrieval time. Failure-derived
        # kinds (tactical / strategic) still search across both -- that's the
        # promotion path the historical pruning already relied on.
        dedup_kind = kind if kind in ("success_pattern", "postmortem") else None
        existing = (
            store.find_by_dedup_key(dedup_key, kind=dedup_kind)
            if dedup_key is not None
            else None
        )
        if existing is None:
            if kind in ("success_pattern", "postmortem"):
                # Both are whole-run signals that dedup against their own kind only,
                # so a "what worked" / "what this run cost" entry never merges into
                # a textually similar failure-derived guideline (which would
                # silently suppress one of the two at retrieval time).
                existing = store.find_similar(text, threshold=similarity_threshold, kind=kind)
            else:
                existing = store.find_similar(text, threshold=similarity_threshold, kind=None)

        if existing is not None:
            entry, score = existing
            if session_id not in entry.session_ids:
                entry.session_ids.append(session_id)
            entry.hit_count += 1
            for tool_name in tools:
                if tool_name not in entry.tools:
                    entry.tools.append(tool_name)
            promoted = len(set(entry.session_ids)) >= promotion_threshold_sessions
            if promoted and entry.kind != "strategic":
                logger.info("promoting guideline to strategic (sim=%.2f): %r", score, entry.text)
                entry.kind = "strategic"
            else:
                logger.debug("merged duplicate guideline (sim=%.2f, hit_count=%d): %r", score, entry.hit_count, entry.text)
            store.upsert(entry)
            return entry

        auto_domain = _classify_domain(text, tools) if kind != "postmortem" else "generic"
        auto_outcome = (
            "success" if kind == "success_pattern"
            else "failure" if kind in ("tactical", "strategic")
            else "unknown"
        )
        entry = MemoryEntry(
            text=text,
            kind=kind,
            session_ids=[session_id],
            tags=tags,
            tools=tools,
            domain=auto_domain,
            outcome=auto_outcome,
            dedup_key=dedup_key,
        )
        logger.debug("inserted new %s guideline: %r tools=%s domain=%s", kind, entry.text, entry.tools, entry.domain)
        store.upsert(entry)

    # Eviction runs outside the per-ingest lock: iterate() is a full scan
    # and holding the lock for it would block concurrent ingests unnecessarily.
    if max_entries is not None:
        evicted = _evict_if_needed(
            store, max_entries, eviction_half_life_days,
            strategy=eviction_strategy,
            llm=eviction_llm,
            guideline_max_tokens=eviction_guideline_max_tokens,
            on_usage=on_eviction_usage,
        )
        if evicted:
            logger.debug("evicted %d entr(ies) to stay within max_entries=%d", evicted, max_entries)

    return entry
