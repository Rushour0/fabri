"""Fixture-specific CI regression checks for retrieval configuration.

This locks in the benchmark discovery (results/benchmarks/retrieval-config-sweep,
2026-07) that tuning top_k / strategy (hybrid vs hybrid+mmr) is not the lever for
a thin memory store -- the ceiling on "relevant guidelines surfaced" is set by
what actually got mined into the corpus, not by how retrieval is configured.

Builds one small in-memory guideline corpus with ~2 entries that are genuinely
relevant to a fixed query and a pile (~18) of topically-distinct distractors --
enough that hybrid fusion and MMR diversification have real ranking work to do,
not a tautological 2-vs-2 pick -- then runs `_retrieve_inner` under multiple
configs and checks two properties on this frozen corpus/query fixture:

  1. top_k (5 vs 10) preserves the relevant-id set for each fixed strategy.
  2. hybrid+mmr does not surface a relevant id that plain hybrid misses.

These assertions protect the published sweep's concrete reproduction. They do
not claim that top_k or MMR are invariant for arbitrary corpora and queries.

Skips cleanly where sqlite-vec isn't installed.
"""
from __future__ import annotations

import hashlib
import math
import re

import pytest

pytest.importorskip("sqlite_vec")  # the sqlite embedded store needs sqlite-vec

from fabri.memory.embedded_store import SqliteMemoryStore
from fabri.memory.embeddings import EMBEDDING_DIM
from fabri.memory.schema import MemoryEntry
from fabri.orchestrator.retrieval import RetrievalConfig, _retrieve_inner

QUERY = "the read_file tool returned a permission denied error on a sandboxed path"

# The only two guidelines in the corpus that are topically about the query
# (file-read permission errors in a sandbox). Everything else below is a
# distractor about an unrelated domain (web search, API auth, planning, git,
# testing, ...) so the retrieval strategies have real ranking work to do --
# a corpus of just 2 relevant docs among 2-3 distractors would be tautological.
RELEVANT_TEXTS = [
    "read_file failing with EACCES on a sandboxed path almost always means the "
    "path resolves outside sandbox_root -- normalize and re-check containment "
    "before retrying.",
    "If a sandboxed agent's file tool call is denied for permissions, the fix is "
    "almost never retrying as-is -- resolve() the path first and confirm it's "
    "still under the sandbox root.",
]

DISTRACTOR_TEXTS = [
    "Always paginate search_web results and cap them at 10 to avoid flooding the context window.",
    "The api endpoint for user auth requires a Bearer token in the Authorization header.",
    "When planning a multi-step task, decompose it into at most five subtasks per level.",
    "Run pytest with -x on the first failing test to shorten the debug loop.",
    "git commit messages should explain why a change was made, not just what changed.",
    "Cache embeddings for tool descriptions at module scope to avoid re-embedding every call.",
    "Rate-limit outbound HTTP requests to third-party APIs to avoid tripping abuse detection.",
    "Prefer structured JSON output from an LLM judge over free-form text for eval scoring.",
    "A retry loop for a flaky network call should use exponential backoff with jitter.",
    "Database migrations should be additive and reversible; never drop a column in the same release.",
    "Long-running background tasks should emit heartbeat events so a supervisor can detect a hang.",
    "Prompt templates should separate system instructions from untrusted user-provided text.",
    "A CLI's --dry-run flag must never perform any side-effecting write.",
    "Log at debug, not error, for a best-effort operation whose failure is already handled.",
    "Feature flags should default to off so a half-shipped code path can't activate accidentally.",
    "Unit tests for a pure function should not require network access or a live database.",
    "Config validation should fail fast at startup, not deep inside a request handler.",
    "A file lock used for a repair loop should be released in a finally block.",
]


def _offline_embed(text: str) -> list[float]:
    """Stable token-hash embedding for this ranking fixture; never downloads."""
    vector = [0.0] * EMBEDDING_DIM
    for token in re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")):
        index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big") % EMBEDDING_DIM
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


@pytest.fixture()
def corpus_store(tmp_path, monkeypatch) -> SqliteMemoryStore:
    import fabri.memory.embedded_store as embedded_store
    import fabri.orchestrator.retrieval as retrieval

    monkeypatch.setattr(embedded_store, "embed", _offline_embed)
    monkeypatch.setattr(retrieval, "embed", _offline_embed)
    store = SqliteMemoryStore(path=tmp_path / "memory.db", collection="invariance_test")
    for text in RELEVANT_TEXTS + DISTRACTOR_TEXTS:
        store.upsert(MemoryEntry(text=text, kind="tactical"))
    # ~20 distinct entries total -- enough distractors that hybrid/MMR actually
    # have ranking work to do rather than trivially returning everything.
    assert store.count() == len(RELEVANT_TEXTS) + len(DISTRACTOR_TEXTS)
    assert store.count() >= 15
    return store


def _relevant_ids() -> set[str]:
    return {MemoryEntry(text=t, kind="tactical").id for t in RELEVANT_TEXTS}


def _retrieved_relevant_ids(
    store: SqliteMemoryStore, relevant_ids: set[str], **kwargs
) -> set[str]:
    _text, merged = _retrieve_inner(store, QUERY, **kwargs)
    return {entry.id for entry, _score in merged} & relevant_ids


# Grouped by strategy so top_k invariance is checked within a fixed strategy --
# hybrid+mmr's diversification legitimately ranks (and can drop) relevant
# candidates differently from plain hybrid; that's a documented trade-off, not
# a bug this fixture-specific gate should chase.
STRATEGY_CONFIGS: dict[str, dict[str, dict]] = {
    "hybrid": {
        "top5": dict(top_k=5, retrieval_config=RetrievalConfig(strategy="hybrid")),
        "top10": dict(top_k=10, retrieval_config=RetrievalConfig(strategy="hybrid")),
    },
    "hybrid+mmr": {
        "top5": dict(top_k=5, retrieval_config=RetrievalConfig(strategy="hybrid+mmr")),
        "top10": dict(top_k=10, retrieval_config=RetrievalConfig(strategy="hybrid+mmr")),
    },
}


def test_top_k_does_not_change_relevant_set_within_a_strategy(corpus_store):
    """Freeze the sweep fixture's observed top_k=5 vs. top_k=10 result."""
    relevant_ids = _relevant_ids()
    assert len(relevant_ids) == 2

    for strategy, by_topk in STRATEGY_CONFIGS.items():
        found = {
            tk_name: _retrieved_relevant_ids(corpus_store, relevant_ids, **kwargs)
            for tk_name, kwargs in by_topk.items()
        }
        top5, top10 = found["top5"], found["top10"]
        assert top5 == top10, (
            f"strategy {strategy!r}: top_k=5 found {top5} but top_k=10 found "
            f"{top10} on the frozen retrieval-sweep fixture"
        )


def test_hybrid_finds_the_full_relevant_set(corpus_store):
    """Sanity check on the corpus construction: the shipped default (hybrid)
    must actually recover BOTH relevant guidelines out of the ~20-entry corpus,
    so the invariance assertions above are checking something non-trivial."""
    relevant_ids = _relevant_ids()
    ids = _retrieved_relevant_ids(corpus_store, relevant_ids, **STRATEGY_CONFIGS["hybrid"]["top5"])
    assert ids == relevant_ids, (
        f"hybrid only surfaced {ids} of the {relevant_ids} relevant guidelines "
        f"in the corpus -- corpus/query fixture needs retuning"
    )


def test_mmr_never_finds_relevant_ids_hybrid_missed(corpus_store):
    """Freeze the sweep fixture's observed hybrid vs. hybrid+mmr result."""
    relevant_ids = _relevant_ids()
    for tk_name in ("top5", "top10"):
        hybrid_ids = _retrieved_relevant_ids(
            corpus_store, relevant_ids, **STRATEGY_CONFIGS["hybrid"][tk_name]
        )
        mmr_ids = _retrieved_relevant_ids(
            corpus_store, relevant_ids, **STRATEGY_CONFIGS["hybrid+mmr"][tk_name]
        )
        assert mmr_ids <= hybrid_ids, (
            f"{tk_name}: hybrid+mmr found relevant ids {mmr_ids} not present in "
            f"plain hybrid's {hybrid_ids} on the frozen retrieval-sweep fixture"
        )
