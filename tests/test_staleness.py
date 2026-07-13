"""Tests for the read-side staleness report (fabri.memory.staleness).

Answers a real open question from production: guidelines get created but
hit_count stays stuck at 1-2 forever, meaning nothing is currently reused.
This module is purely additive -- it reads existing fields (hit_count,
created_at) via the same store.iterate() interface pruning.py already uses,
and does not touch scoring, retrieval, or eviction. See tests/test_retrieval.py
for the store fixture conventions this file mirrors."""
import argparse
import json
import time
import uuid

import pytest

from fabri import cli
from fabri.memory.schema import MemoryEntry
from fabri.memory.staleness import StaleGuideline, find_stale_guidelines
from fabri.memory.store import QdrantMemoryStore

DAY = 86_400.0


def make_store() -> QdrantMemoryStore:
    return QdrantMemoryStore(collection=f"test_stale_{uuid.uuid4().hex[:8]}")


def _entry(text: str, kind: str, hit_count: int, age_days: float, **kw) -> MemoryEntry:
    return MemoryEntry(
        text=text,
        kind=kind,
        hit_count=hit_count,
        created_at=time.time() - age_days * DAY,
        **kw,
    )


def test_old_and_low_hit_count_guideline_is_stale():
    store = make_store()
    stale = _entry("Rarely reused guideline.", "tactical", hit_count=1, age_days=30)
    store.upsert(stale)

    results = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0)

    assert [r.text for r in results] == [stale.text]
    assert isinstance(results[0], StaleGuideline)
    assert results[0].hit_count == 1
    assert results[0].kind == "tactical"

    store.delete(stale.id)


def test_young_low_hit_guideline_is_excluded_too_new_to_judge():
    store = make_store()
    fresh = _entry("Just created, not yet reused.", "tactical", hit_count=1, age_days=1)
    store.upsert(fresh)

    results = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0)

    assert results == []

    store.delete(fresh.id)


def test_high_hit_count_guideline_excluded_regardless_of_age():
    store = make_store()
    popular = _entry("Reused constantly.", "tactical", hit_count=50, age_days=365)
    store.upsert(popular)

    results = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0)

    assert results == []

    store.delete(popular.id)


def test_kind_filter_excludes_kinds_not_requested():
    store = make_store()
    tactical_stale = _entry("Stale tactical.", "tactical", hit_count=1, age_days=30)
    postmortem_stale = _entry("Stale postmortem.", "postmortem", hit_count=1, age_days=30)
    store.upsert(tactical_stale)
    store.upsert(postmortem_stale)

    # Default kinds=("tactical", "strategic") should skip the postmortem entry.
    results = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0)
    assert [r.text for r in results] == [tactical_stale.text]

    # Explicitly widening the kinds filter picks it up.
    results_all = find_stale_guidelines(
        store, max_hit_count=2, min_age_days=7.0, kinds=("tactical", "strategic", "postmortem")
    )
    assert {r.text for r in results_all} == {tactical_stale.text, postmortem_stale.text}

    store.delete(tactical_stale.id)
    store.delete(postmortem_stale.id)


def test_results_sorted_oldest_least_used_first():
    store = make_store()
    older = _entry("Older, less used.", "tactical", hit_count=1, age_days=60)
    newer = _entry("Newer, still stale.", "tactical", hit_count=2, age_days=10)
    # Upsert in an order that would NOT already match the expected sort.
    store.upsert(newer)
    store.upsert(older)

    results = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0)

    assert [r.text for r in results] == [older.text, newer.text]

    store.delete(older.id)
    store.delete(newer.id)


def test_limit_truncates_results():
    store = make_store()
    entries = [
        _entry(f"Stale guideline {i}.", "tactical", hit_count=1, age_days=30 + i)
        for i in range(5)
    ]
    for e in entries:
        store.upsert(e)

    results = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0, limit=2)

    assert len(results) == 2

    for e in entries:
        store.delete(e.id)


def test_stale_guideline_projection_is_a_small_shape_not_the_raw_entry():
    store = make_store()
    stale = _entry(
        "Projection shape check.", "tactical", hit_count=1, age_days=30,
        domain="code", tags=["foo"], session_ids=["s1"],
    )
    store.upsert(stale)

    [result] = find_stale_guidelines(store, max_hit_count=2, min_age_days=7.0)

    assert result.id == stale.id
    assert result.domain == "code"
    assert result.tags == ["foo"]
    assert result.session_ids == ["s1"]
    assert result.created_at == stale.created_at
    assert result.age_days >= 30

    store.delete(stale.id)


def test_cli_memory_stale_smoke_produces_valid_json(monkeypatch, capsys):
    """Matches tests/test_cli_run_exit_code.py's pattern of monkeypatching
    cli.load_config / cli._open_store so the command under test is exercised
    without a networked qdrant fixture in the config path."""
    store = make_store()
    stale = _entry("CLI smoke stale guideline.", "tactical", hit_count=1, age_days=30)
    store.upsert(stale)

    monkeypatch.setattr(cli, "load_config", lambda _p: {
        "memory": {
            "qdrant_url": "x", "collection": "c", "backend": "qdrant",
            "global_collection": None,
            "stale_max_hit_count": 2, "stale_min_age_days": 7.0,
        },
    })
    monkeypatch.setattr(cli, "_open_store", lambda _cfg: store)

    args = argparse.Namespace(
        config=None, max_hit_count=None, min_age_days=None, kind=None, limit=None,
    )
    cli.cmd_memory_stale(args)

    out = capsys.readouterr().out
    payload = json.loads(out)

    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["text"] == stale.text
    assert payload[0]["hit_count"] == 1

    store.delete(stale.id)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
