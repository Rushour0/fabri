"""CI gate: fabri's dense retrieval must not regress below a fixed baseline.

Runs the fast, offline retrieval eval (see
`src/fabri/benchmarks/retrieval_eval/`) and asserts the DENSE strategy — the
shipped default — still clears the floors captured from the first measured
baseline. This is a regression tripwire, not an aspirational quality bar: it
catches a change that silently makes retrieval worse, without demanding it be
perfect.

Baseline (re-measured 2026-07-07 after the RRF-k retune + success-slot
back-load, MiniLM-L6-v2, tests/fixtures/retrieval_eval.json, 40 guidelines /
24 queries, top_k=5):
    dense       recall@5 = 0.7917  recall@3 = 0.6875  mrr = 0.7896
    hybrid      recall@5 = 0.9375  recall@3 = 0.8958  mrr = 0.8438  (default)
    hybrid+mmr  recall@5 = 0.729                       mrr = 0.804

Floors are baseline − 0.05 (absorbs float jitter; never exact equality — that
was the shape of the July 2026 CI flakiness). Regenerate the baseline with
`python -m fabri.benchmarks.retrieval_eval` and bump the floors only with a
committed results note explaining the move.

Skips cleanly where sqlite-vec isn't installed (`pip install -e ".[dev,sqlite]"`).
"""
import pytest

pytest.importorskip("sqlite_vec")  # the eval store needs sqlite-vec; skip if absent

from fabri.benchmarks.retrieval_eval import run_eval

# Baselines re-measured 2026-07-07 after two retrieval fixes (RRF k 60->20 and
# success-slot back-load; both lifted rank-1/@3 markedly):
#   dense   recall@5 = 0.7917  recall@3 = 0.6875  mrr = 0.7896
#   hybrid  recall@5 = 0.9375  recall@3 = 0.8958  mrr = 0.8438   (shipped default)
# Floors are baseline − 0.05. The MRR floor jumped from 0.39: the back-load fix
# stopped success patterns stealing rank 1, so a regression to the old MRR must
# now trip the gate.
DENSE_RECALL5_FLOOR = 0.74
DENSE_RECALL3_FLOOR = 0.63
DENSE_MRR_FLOOR = 0.74
HYBRID_RECALL5_FLOOR = 0.89  # protects the shipped default strategy
HYBRID_RECALL3_FLOOR = 0.84  # back-load win — must not silently regress
HYBRID_MRR_FLOOR = 0.79      # back-load win — must not silently regress

# "hybrid+mmr" (published: recall@5 0.729, mrr 0.804) — note the literal `+`;
# a `hybrid_mmr` underscore typo would silently fall through to `dense`
# (both runner.py and orchestrator/retrieval.py match strategies via
# `"mmr" in strategy` / exact-string membership, not this specific spelling).
HYBRID_MMR_RECALL5_FLOOR = 0.68
HYBRID_MMR_MRR_FLOOR = 0.75


@pytest.fixture(scope="module")
def eval_results(tmp_path_factory):
    """Run dense + hybrid + hybrid+mmr once for the whole module (embeds the corpus once)."""
    tmp = tmp_path_factory.mktemp("retrieval_eval")
    return run_eval(strategies=["dense", "hybrid", "hybrid+mmr"], tmp_dir=tmp)


@pytest.fixture(scope="module")
def dense_results(eval_results):
    return eval_results["dense"]


def test_dense_recall_at_5_meets_baseline(dense_results):
    assert dense_results["recall@5"] >= DENSE_RECALL5_FLOOR, (
        f"dense recall@5 regressed to {dense_results['recall@5']} "
        f"(floor {DENSE_RECALL5_FLOOR}); investigate before merging"
    )


def test_dense_recall_at_3_meets_baseline(dense_results):
    assert dense_results["recall@3"] >= DENSE_RECALL3_FLOOR, (
        f"dense recall@3 regressed to {dense_results['recall@3']} (floor {DENSE_RECALL3_FLOOR})"
    )


def test_dense_mrr_meets_baseline(dense_results):
    assert dense_results["mrr"] >= DENSE_MRR_FLOOR, (
        f"dense MRR regressed to {dense_results['mrr']} (floor {DENSE_MRR_FLOOR})"
    )


def test_hybrid_default_recall_at_5_meets_baseline(eval_results):
    """hybrid is the shipped default (D3) — protect its recall@5 floor."""
    got = eval_results["hybrid"]["recall@5"]
    assert got >= HYBRID_RECALL5_FLOOR, (
        f"default (hybrid) recall@5 regressed to {got} (floor {HYBRID_RECALL5_FLOOR})"
    )


def test_hybrid_default_recall_at_3_meets_baseline(eval_results):
    """The RRF-k retune lifted hybrid recall@3 0.60 -> 0.90 — lock it in."""
    got = eval_results["hybrid"]["recall@3"]
    assert got >= HYBRID_RECALL3_FLOOR, (
        f"default (hybrid) recall@3 regressed to {got} (floor {HYBRID_RECALL3_FLOOR}) "
        f"— check RRF k (RetrievalConfig.rrf_k) or the success-slot back-load"
    )


def test_hybrid_default_mrr_meets_baseline(eval_results):
    """The success-slot back-load lifted hybrid MRR 0.45 -> 0.84 — lock it in."""
    got = eval_results["hybrid"]["mrr"]
    assert got >= HYBRID_MRR_FLOOR, (
        f"default (hybrid) MRR regressed to {got} (floor {HYBRID_MRR_FLOOR}) "
        f"— success patterns may be stealing rank 1 again (check the merge order)"
    )


def test_hybrid_mmr_recall_at_5_meets_baseline(eval_results):
    """Locks in the "hybrid+mmr" strategy (confirm the exact literal string —
    an underscore variant silently falls through to dense)."""
    got = eval_results["hybrid+mmr"]["recall@5"]
    assert got >= HYBRID_MMR_RECALL5_FLOOR, (
        f"hybrid+mmr recall@5 regressed to {got} (floor {HYBRID_MMR_RECALL5_FLOOR})"
    )


def test_hybrid_mmr_mrr_meets_baseline(eval_results):
    got = eval_results["hybrid+mmr"]["mrr"]
    assert got >= HYBRID_MMR_MRR_FLOOR, (
        f"hybrid+mmr MRR regressed to {got} (floor {HYBRID_MMR_MRR_FLOOR})"
    )


def test_eval_is_deterministic(tmp_path):
    """Two runs of the same fixture must give identical numbers — a moving gate
    is a useless gate. Guards against nondeterministic tie-breaking sneaking in."""
    a = run_eval(strategies=["dense"], tmp_dir=tmp_path / "a")["dense"]
    b = run_eval(strategies=["dense"], tmp_dir=tmp_path / "b")["dense"]
    assert a == b


def test_hybrid_bm25_is_alive(tmp_path):
    """Regression guard for the FTS5 implicit-AND bug: on the SQLite backend,
    `sparse`/`hybrid` retrieval must actually beat `dense`. If BM25 silently
    dies again (e.g. `_fts5_query` reverts to a space-join), hybrid collapses
    back to dense and this trips. See docs/design/memory-observability-plan.md."""
    res = run_eval(strategies=["dense", "hybrid"], tmp_dir=tmp_path)
    assert res["hybrid"]["recall@5"] > res["dense"]["recall@5"], (
        f"hybrid recall@5 ({res['hybrid']['recall@5']}) did not beat dense "
        f"({res['dense']['recall@5']}) — BM25 may have silently degraded to a no-op"
    )
