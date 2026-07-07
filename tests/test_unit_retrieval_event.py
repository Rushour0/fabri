"""M3: the `retrieval` trace event emitted by _retrieve_inner.

Uses a MagicMock store + a monkeypatched `embed` so nothing hits Qdrant or
downloads the 44MB sentence-transformers model, and isolates trace files via
FABRI_HOME -> tmp (the autouse conftest fixture only isolates Qdrant
collections, not trace files). See docs/design/memory-observability-plan.md (A).
"""
from unittest.mock import MagicMock

import pytest

from fabri.memory.schema import MemoryEntry
from fabri.orchestrator.retrieval import RetrievalConfig, _retrieve_inner
from fabri.orchestrator.traces import read_trace


@pytest.fixture(autouse=True)
def _isolate_traces(tmp_path, monkeypatch):
    """Point FABRI_HOME at a tmp dir so emitted trace files don't leak."""
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    # Fixed 384-dim unit-ish vector so retrieval never loads the real model.
    monkeypatch.setattr(
        "fabri.orchestrator.retrieval.embed", lambda text: [0.1] * 384
    )


def _entry(text: str, kind: str = "tactical") -> MemoryEntry:
    return MemoryEntry(text=text, kind=kind)


def _dense_store(n: int = 3):
    """MagicMock store with `n` dense hits and NO usable BM25 (returns [])."""
    store = MagicMock()
    store.count.return_value = n
    entries = [_entry(f"guideline number {i} about files") for i in range(n)]
    store.query_by_vector.return_value = [
        (entries[i], 0.9 - 0.1 * i) for i in range(n)
    ]
    store.query_bm25.return_value = []  # MagicMock auto-creates the attr; make it benign
    return store


def _retrieval_events(session_id: str) -> list[dict]:
    return [e for e in read_trace(session_id) if e.get("type") == "retrieval"]


def test_dense_strategy_emits_event_without_nameerror():
    # The dense path never enters the sparse branch — this is the regression
    # guard for the `sparse_results` NameError the plan flagged.
    store = _dense_store()
    _retrieve_inner(
        store, "read a file", top_k=3,
        retrieval_config=RetrievalConfig(strategy="dense"),
        session_id="sess-dense",
    )
    events = _retrieval_events("sess-dense")
    assert len(events) == 1
    ev = events[0]
    assert ev["strategy"] == "dense"
    assert ev["sparse_pool_size"] == 0
    assert ev["sparse_backend"] is None
    assert ev["sparse_fallback"] is False
    assert ev["retrieved"] == 3
    assert ev["dense_pool_size"] == 3
    assert len(ev["candidates"]) == 3
    assert {c["inclusion_reason"] for c in ev["candidates"]} == {"base"}
    assert ev["score_max"] >= ev["score_min"]


def test_hybrid_strategy_records_bm25_backend():
    store = _dense_store()
    store.query_bm25.return_value = [(_entry("bm25 hit about files"), 2.1)]
    _retrieve_inner(
        store, "read a file", top_k=3,
        retrieval_config=RetrievalConfig(strategy="hybrid"),
        session_id="sess-hybrid",
    )
    ev = _retrieval_events("sess-hybrid")[0]
    assert ev["strategy"] == "hybrid"
    assert ev["sparse_backend"] == "fts5"
    assert ev["sparse_pool_size"] == 1
    assert ev["sparse_fallback"] is False


def test_hybrid_records_silent_fallback_when_bm25_empty():
    # hybrid requested but BM25 returns nothing -> we used dense; the event must
    # make that visible (the "hybrid is secretly dense" degradation).
    store = _dense_store()
    store.query_bm25.return_value = []
    _retrieve_inner(
        store, "read a file", top_k=3,
        retrieval_config=RetrievalConfig(strategy="hybrid"),
        session_id="sess-fallback",
    )
    ev = _retrieval_events("sess-fallback")[0]
    assert ev["sparse_fallback"] is True
    assert ev["sparse_pool_size"] == 0


def test_session_id_none_emits_nothing(tmp_path):
    from fabri.paths import traces_dir

    store = _dense_store()
    _retrieve_inner(
        store, "read a file", top_k=3,
        retrieval_config=RetrievalConfig(strategy="dense"),
        session_id=None,
    )
    assert list(traces_dir().glob("*.jsonl")) == []


def test_cold_store_emits_minimal_event():
    store = MagicMock()
    store.count.return_value = 0
    _retrieve_inner(store, "anything", top_k=3, session_id="sess-cold")
    ev = _retrieval_events("sess-cold")[0]
    assert ev["store_count"] == 0
    assert ev["retrieved"] == 0
    assert ev["cold_store"] is True


def test_success_pattern_does_not_steal_rank_one():
    # Back-load regression guard: a success pattern that is NOT the most relevant
    # must land in a reserved TAIL slot, never rank 1 — while still being
    # guaranteed into the injected set. Front-loading it (the pre-fix behaviour)
    # sank recall@1 0.60 -> 0.13 in the offline eval.
    store = MagicMock()
    store.count.return_value = 5
    tac1 = _entry("most relevant tactical", "tactical")
    tac2 = _entry("second tactical", "tactical")
    tac3 = _entry("third tactical", "tactical")
    tac4 = _entry("fourth tactical", "tactical")
    succ = _entry("a low-relevance success pattern", "success_pattern")
    # dense order puts the success pattern LAST (least relevant to the query).
    store.query_by_vector.return_value = [
        (tac1, 0.9), (tac2, 0.8), (tac3, 0.7), (tac4, 0.6), (succ, 0.2),
    ]
    store.query_bm25.return_value = []
    _retrieve_inner(
        store, "read a file", top_k=3,
        retrieval_config=RetrievalConfig(strategy="dense"),
        session_id="sess-backload",
    )
    ev = _retrieval_events("sess-backload")[0]
    cands = ev["candidates"]
    # rank 1 is the most-relevant tactical, NOT the forced success pattern
    assert cands[0]["id"] == tac1.id
    assert cands[0]["inclusion_reason"] == "base"
    # ...but the success pattern is still guaranteed into the injected set (tail)
    assert any(c["kind"] == "success_pattern" for c in cands)
    assert cands[-1]["kind"] == "success_pattern"


def test_event_reconstructs_injected_set():
    # The observability must not lie: the candidate ids in the event must be
    # exactly the entries that were injected (returned) — not a superset.
    store = _dense_store(n=5)
    _text, merged = _retrieve_inner(
        store, "read a file", top_k=3,
        retrieval_config=RetrievalConfig(strategy="dense"),
        session_id="sess-recon",
    )
    ev = _retrieval_events("sess-recon")[0]
    assert [c["id"] for c in ev["candidates"]] == [e.id for e, _ in merged]
