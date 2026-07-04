"""Unit tests for memory eviction (max_entries + time-weighted scoring).

These tests run without a live Qdrant instance using a minimal stub store,
so they are fast and runnable in CI without infrastructure.
"""
import math
import time
from dataclasses import dataclass, field

import pytest

from fabri.memory.pruning import _eviction_score, _evict_if_needed, _summarize_and_evict
from fabri.memory.schema import MemoryEntry


# ---------------------------------------------------------------------------
# Minimal stub store — no embeddings, no Qdrant, no sqlite-vec
# ---------------------------------------------------------------------------

class _StubStore:
    """In-memory store that satisfies the iterate/delete/count/upsert interface."""

    def __init__(self, entries: list[MemoryEntry]):
        self._entries: dict[str, MemoryEntry] = {e.id: e for e in entries}
        # Make every entry's id unique for the stub (override property)
        # by using a running integer string as the key.
        self._entries = {}
        for i, e in enumerate(entries):
            self._entries[str(i)] = e
            # Patch the id so delete() can find it
            object.__setattr__(e, "_stub_id", str(i))
        self._next_key = len(entries)

    def count(self, kind=None):
        if kind is None:
            return len(self._entries)
        return sum(1 for e in self._entries.values() if e.kind == kind)

    def iterate(self, kind=None, limit=None):
        out = list(self._entries.values())
        if kind is not None:
            out = [e for e in out if e.kind == kind]
        if limit is not None:
            out = out[:limit]
        return out

    def upsert(self, entry: MemoryEntry) -> None:
        key = str(self._next_key)
        self._next_key += 1
        object.__setattr__(entry, "_stub_id", key)
        self._entries[key] = entry

    def delete(self, point_id: str) -> None:
        # Match by stub id (patched above)
        for k, e in list(self._entries.items()):
            if getattr(e, "_stub_id", None) == point_id:
                del self._entries[k]
                return
        # Fallback: try the key directly
        self._entries.pop(point_id, None)


def _make_entry(kind="tactical", hit_count=1, age_days=0.0) -> MemoryEntry:
    created_at = time.time() - age_days * 86_400.0
    e = MemoryEntry(
        text=f"guideline {kind} hc={hit_count} age={age_days}",
        kind=kind,
        hit_count=hit_count,
        created_at=created_at,
    )
    return e


# ---------------------------------------------------------------------------
# _eviction_score
# ---------------------------------------------------------------------------

def test_eviction_score_decreases_with_age():
    fresh = _make_entry(age_days=0)
    old = _make_entry(age_days=30)
    assert _eviction_score(fresh, half_life_days=30) > _eviction_score(old, half_life_days=30)


def test_eviction_score_increases_with_hit_count():
    low = _make_entry(hit_count=1, age_days=1)
    high = _make_entry(hit_count=10, age_days=1)
    assert _eviction_score(high, half_life_days=30) > _eviction_score(low, half_life_days=30)


def test_eviction_score_at_half_life():
    entry = _make_entry(hit_count=2, age_days=30)
    score = _eviction_score(entry, half_life_days=30)
    # score ≈ 2 * 0.5 = 1.0 at half-life
    assert abs(score - 1.0) < 0.05


def test_eviction_score_fresh_entry_equals_hit_count():
    entry = _make_entry(hit_count=5, age_days=0)
    score = _eviction_score(entry, half_life_days=30)
    assert abs(score - 5.0) < 0.1


# ---------------------------------------------------------------------------
# _evict_if_needed
# ---------------------------------------------------------------------------

def test_no_eviction_when_under_limit():
    entries = [_make_entry("tactical", hit_count=1, age_days=i) for i in range(3)]
    store = _StubStore(entries)
    evicted = _evict_if_needed(store, max_entries=5, half_life_days=30)
    assert evicted == 0
    assert store.count() == 3


def test_evicts_lowest_scored_first():
    # Entry 0: old + low hits → lowest score (should be evicted)
    # Entry 1: fresh + high hits → highest score (should survive)
    old_low = _make_entry("tactical", hit_count=1, age_days=120)
    fresh_high = _make_entry("tactical", hit_count=10, age_days=0)
    store = _StubStore([old_low, fresh_high])
    evicted = _evict_if_needed(store, max_entries=1, half_life_days=30)
    assert evicted == 1
    assert store.count() == 1
    surviving = store.iterate()[0]
    assert "hc=10" in surviving.text


def test_strategic_entries_protected_until_last():
    strategic = _make_entry("strategic", hit_count=1, age_days=200)
    tactical = _make_entry("tactical", hit_count=1, age_days=200)
    store = _StubStore([strategic, tactical])
    evicted = _evict_if_needed(store, max_entries=1, half_life_days=30)
    assert evicted == 1
    assert store.count() == 1
    surviving = store.iterate()[0]
    assert surviving.kind == "strategic"


def test_evicts_multiple_to_reach_limit():
    entries = [_make_entry("tactical", hit_count=1, age_days=30 * i) for i in range(5)]
    store = _StubStore(entries)
    evicted = _evict_if_needed(store, max_entries=2, half_life_days=30)
    assert evicted == 3
    assert store.count() == 2


def test_evicts_strategic_when_only_option():
    entries = [_make_entry("strategic", hit_count=1, age_days=i * 10) for i in range(3)]
    store = _StubStore(entries)
    evicted = _evict_if_needed(store, max_entries=1, half_life_days=30)
    assert evicted == 2
    assert store.count() == 1


# ---------------------------------------------------------------------------
# Summarize-before-evict strategy
# ---------------------------------------------------------------------------

class _ScriptedLLM:
    """Minimal LLM stub that returns a fixed string for every step call."""

    def __init__(self, text: str = "compressed guideline"):
        self._text = text
        self.calls: list[str] = []

    def step(self, system: str, messages: list) -> object:
        prompt = messages[-1]["content"] if messages else ""
        self.calls.append(prompt)

        class _Resp:
            final_text = None
            usage = None

        _Resp.final_text = self._text
        return _Resp()


def test_summarize_strategy_calls_llm_and_upserts():
    """_evict_if_needed with strategy='summarize' should call the LLM once per
    chunk and upsert a compressed entry in place of the evicted ones."""
    entries = [_make_entry("tactical", hit_count=1, age_days=30 * i) for i in range(3)]
    store = _StubStore(entries)
    llm = _ScriptedLLM("always validate args before calling the tool")

    evicted = _evict_if_needed(
        store, max_entries=1, half_life_days=30,
        strategy="summarize", llm=llm, guideline_max_tokens=30,
    )

    assert evicted == 2
    # 2 originals deleted, 1 summary upserted → net 1 entry in store
    assert store.count() == 2  # 1 surviving original + 1 summary
    assert llm.calls, "LLM should have been called at least once"
    texts = [e.text for e in store.iterate()]
    assert any("always validate" in t for t in texts), f"summary not found in {texts}"


def test_summarize_strategy_preserves_strategic_kind():
    """A strategic entry summarized via the eviction path should stay strategic."""
    strategics = [_make_entry("strategic", hit_count=1, age_days=200) for _ in range(3)]
    store = _StubStore(strategics)
    llm = _ScriptedLLM("compressed strategic rule")

    _evict_if_needed(
        store, max_entries=1, half_life_days=30,
        strategy="summarize", llm=llm, guideline_max_tokens=30,
    )

    surviving = store.iterate()
    # Some originals were summarized; all resulting entries should be strategic.
    assert all(e.kind == "strategic" for e in surviving), (
        f"expected all strategic, got {[e.kind for e in surviving]}"
    )


def test_summarize_strategy_falls_back_to_delete_on_llm_error():
    """If the LLM raises during summarization, the group is plain-deleted."""
    class _BrokenLLM:
        def step(self, *a, **kw):
            raise RuntimeError("LLM unavailable")

    entries = [_make_entry("tactical", hit_count=1, age_days=30 * i) for i in range(3)]
    store = _StubStore(entries)

    # Should not raise; falls back to delete for each failing group.
    evicted = _evict_if_needed(
        store, max_entries=1, half_life_days=30,
        strategy="summarize", llm=_BrokenLLM(), guideline_max_tokens=30,
    )

    assert evicted == 2
    # Fallback deleted the originals; no summary was upserted.
    assert store.count() == 1


def test_summarize_strategy_with_no_llm_falls_back_to_delete():
    """strategy='summarize' + llm=None must behave identically to strategy='delete'."""
    entries = [_make_entry("tactical", hit_count=1, age_days=30 * i) for i in range(3)]
    store = _StubStore(entries)

    evicted = _evict_if_needed(
        store, max_entries=1, half_life_days=30,
        strategy="summarize", llm=None,
    )

    assert evicted == 2
    assert store.count() == 1


def test_summarize_groups_large_batch():
    """More than _SUMMARY_CHUNK entries should produce multiple LLM calls."""
    from fabri.memory.pruning import _SUMMARY_CHUNK
    n = _SUMMARY_CHUNK + 2  # forces 2 chunks
    entries = [_make_entry("tactical", hit_count=1, age_days=30 * i) for i in range(n + 1)]
    store = _StubStore(entries)
    llm = _ScriptedLLM("multi-chunk summary")

    _evict_if_needed(
        store, max_entries=1, half_life_days=30,
        strategy="summarize", llm=llm, guideline_max_tokens=30,
    )

    # Two chunks → two LLM calls
    assert len(llm.calls) >= 2
