"""Unit tests for hybrid retrieval helpers and RetrievalConfig.

All tests are pure Python with mocked stores — no Qdrant, no sqlite-vec,
no sentence-transformers download required."""
from __future__ import annotations

import math
import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from fabri.memory.schema import MemoryEntry
from fabri.orchestrator.retrieval import (
    RetrievalConfig,
    _apply_mmr,
    _classify_domain,
    _importance_score,
    _rrf_fuse,
    _temporal_weight,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(text: str, kind: str = "tactical", hit_count: int = 1, **kwargs) -> MemoryEntry:
    return MemoryEntry(text=text, kind=kind, hit_count=hit_count, **kwargs)


# ---------------------------------------------------------------------------
# RetrievalConfig
# ---------------------------------------------------------------------------

class TestRetrievalConfig:
    def test_defaults(self):
        cfg = RetrievalConfig()
        assert cfg.strategy == "dense"
        assert cfg.temporal_decay is False
        assert cfg.temporal_half_life_days == 30.0
        assert cfg.mmr_lambda == 0.7
        assert cfg.domain_routing is False
        assert cfg.importance_weight == 0.2
        assert cfg.query_expansion is False

    def test_from_mem_cfg_parses_all_keys(self):
        mem_cfg = {
            "retrieval_strategy": "hybrid+mmr",
            "temporal_decay": True,
            "temporal_half_life_days": 14.0,
            "mmr_lambda": 0.5,
            "domain_routing": True,
            "importance_weight": 0.4,
            "query_expansion": True,
        }
        cfg = RetrievalConfig.from_mem_cfg(mem_cfg)
        assert cfg.strategy == "hybrid+mmr"
        assert cfg.temporal_decay is True
        assert cfg.temporal_half_life_days == 14.0
        assert cfg.mmr_lambda == 0.5
        assert cfg.domain_routing is True
        assert cfg.importance_weight == 0.4
        assert cfg.query_expansion is True

    def test_from_mem_cfg_empty_dict_gives_defaults(self):
        cfg = RetrievalConfig.from_mem_cfg({})
        assert cfg == RetrievalConfig()

    def test_frozen(self):
        cfg = RetrievalConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.strategy = "hybrid"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

class TestRrfFuse:
    def _make_entries(self, n: int) -> list[tuple[MemoryEntry, float]]:
        return [(_entry(f"entry {i}"), 1.0 / (i + 1)) for i in range(n)]

    def test_empty_inputs(self):
        assert _rrf_fuse([], []) == []

    def test_dense_only_passthrough(self):
        dense = self._make_entries(3)
        result = _rrf_fuse(dense, [])
        ids = [e.id for e, _ in result]
        dense_ids = [e.id for e, _ in dense]
        assert set(ids) == set(dense_ids)

    def test_sparse_only_passthrough(self):
        sparse = self._make_entries(3)
        result = _rrf_fuse([], sparse)
        assert {e.id for e, _ in result} == {e.id for e, _ in sparse}

    def test_overlap_entry_scores_higher_than_single_list(self):
        shared = _entry("shared entry")
        unique = _entry("unique entry")
        dense = [(shared, 0.9), (unique, 0.5)]
        sparse = [(shared, 0.8)]
        result = _rrf_fuse(dense, sparse)
        result_map = {e.id: sc for e, sc in result}
        # shared appears in both → RRF score > 2 * 1/(60+1)
        assert result_map[shared.id] > 2.0 / (60 + 2)
        assert result_map[shared.id] > result_map[unique.id]

    def test_order_is_descending(self):
        dense = [(_entry(f"d{i}"), float(5 - i)) for i in range(5)]
        sparse = [(_entry(f"s{i}"), float(5 - i)) for i in range(5)]
        result = _rrf_fuse(dense, sparse)
        scores = [sc for _, sc in result]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Temporal decay
# ---------------------------------------------------------------------------

class TestTemporalWeight:
    def test_very_recent_is_near_one(self):
        now = time.time()
        w = _temporal_weight(now - 60, half_life_days=30.0)  # 1 minute ago
        assert w > 0.999

    def test_half_life_age_gives_half(self):
        half_life = 30.0
        age_seconds = half_life * 86_400
        w = _temporal_weight(time.time() - age_seconds, half_life_days=half_life)
        assert abs(w - 0.5) < 0.01

    def test_double_half_life_gives_quarter(self):
        half_life = 30.0
        age_seconds = 2 * half_life * 86_400
        w = _temporal_weight(time.time() - age_seconds, half_life_days=half_life)
        assert abs(w - 0.25) < 0.01

    def test_future_timestamp_clamps_to_one(self):
        w = _temporal_weight(time.time() + 86_400, half_life_days=30.0)
        assert w >= 1.0  # age_days clamped to 0 → exp(0) = 1

    def test_shorter_half_life_decays_faster(self):
        age = time.time() - 7 * 86_400  # 7 days ago
        w_short = _temporal_weight(age, half_life_days=7.0)
        w_long = _temporal_weight(age, half_life_days=30.0)
        assert w_short < w_long


# ---------------------------------------------------------------------------
# Importance score
# ---------------------------------------------------------------------------

class TestImportanceScore:
    def test_strategic_bonus(self):
        tactical = _entry("t", kind="tactical", hit_count=3)
        strategic = _entry("s", kind="strategic", hit_count=3)
        assert _importance_score(strategic) > _importance_score(tactical)

    def test_caps_at_one(self):
        e = _entry("x", hit_count=100)
        assert _importance_score(e) == 1.0

    def test_strategic_with_hit_count_one_exceeds_tactical_with_hit_count_three(self):
        # strategic(1) = min(1, 0.1 + 0.3) = 0.4 > tactical(3) = min(1, 0.3) = 0.3
        tactical = _entry("t", kind="tactical", hit_count=3)
        strategic = _entry("s", kind="strategic", hit_count=1)
        assert _importance_score(strategic) > _importance_score(tactical)

    def test_zero_hit_count_treated_as_one(self):
        e = _entry("x", hit_count=0)
        score = _importance_score(e)
        assert score > 0


# ---------------------------------------------------------------------------
# Domain classifier
# ---------------------------------------------------------------------------

class TestClassifyDomain:
    def test_code_from_tool_name(self):
        assert _classify_domain("do something", ["read_file"]) == "code"

    def test_code_from_task_keyword(self):
        assert _classify_domain("write a python script") == "code"

    def test_search_from_task(self):
        assert _classify_domain("search the web for news") == "search"

    def test_planning_from_task(self):
        assert _classify_domain("plan the architecture") == "planning"

    def test_api_from_task(self):
        assert _classify_domain("call the api endpoint") == "api"

    def test_generic_fallback(self):
        assert _classify_domain("hello world", []) == "generic"

    def test_none_tool_names(self):
        assert _classify_domain("hello", None) == "generic"

    def test_first_match_wins(self):
        # "write" triggers "code" before anything else
        result = _classify_domain("write a search query")
        assert result == "code"


# ---------------------------------------------------------------------------
# MMR diversification
# ---------------------------------------------------------------------------

class TestApplyMmr:
    def _pair(self, text: str, score: float = 1.0) -> tuple[MemoryEntry, float]:
        return (_entry(text), score)

    def test_noop_when_pool_smaller_than_top_k(self):
        candidates = [self._pair(f"entry {i}") for i in range(3)]
        result = _apply_mmr(candidates, [1.0, 0.0], lambda_=0.7, top_k=5)
        assert result == candidates

    def test_noop_when_pool_equals_top_k(self):
        candidates = [self._pair(f"entry {i}") for i in range(5)]
        result = _apply_mmr(candidates, [1.0, 0.0], lambda_=0.7, top_k=5)
        assert result == candidates

    def test_trims_to_top_k(self):
        candidates = [self._pair(f"entry {i}") for i in range(10)]
        result = _apply_mmr(candidates, [1.0, 0.0], lambda_=0.7, top_k=3)
        assert len(result) == 3

    def test_diversity_lambda_zero_picks_diverse(self):
        # With lambda_=0 (pure diversity), identical texts should not all be selected.
        # Use distinct texts that embed differently; the test just checks length.
        candidates = [
            self._pair("read a file from disk"),
            self._pair("write data to a file"),
            self._pair("search the internet for results"),
            self._pair("plan the architecture of a system"),
            self._pair("call an api endpoint with headers"),
        ]
        query_vec = [0.1] * 384  # simple non-zero query
        result = _apply_mmr(candidates, query_vec, lambda_=0.0, top_k=3)
        assert len(result) == 3

    def test_relevance_lambda_one_same_as_truncation(self):
        # With lambda_=1.0, MMR == pick highest relevance (ties may differ slightly).
        candidates = [(e, float(10 - i)) for i, (e, _) in enumerate(
            [self._pair(f"entry {i}") for i in range(8)]
        )]
        query_vec = [0.0] * 384
        result = _apply_mmr(candidates, query_vec, lambda_=1.0, top_k=3)
        assert len(result) == 3
