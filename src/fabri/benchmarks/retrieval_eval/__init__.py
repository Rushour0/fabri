"""Offline retrieval-quality eval for fabri's memory retrieval.

A fast, deterministic, credit-free eval that runs fabri's real `_retrieve_inner`
over a labeled fixture and reports recall@k / MRR / precision@k. Unlike the
`session_delta` (cost) and `longmemeval` (end-to-end recall) runners — which
need API credits and hours — this one runs in CI in seconds and gates retrieval
changes against a fixed baseline.

    python -m fabri.benchmarks.retrieval_eval            # head-to-head table
    python -m fabri.benchmarks.retrieval_eval --json     # machine-readable

See `docs/design/memory-observability-plan.md` (unit C).
"""
from fabri.benchmarks.retrieval_eval.runner import FIXTURE_PATH, run_eval

__all__ = ["run_eval", "FIXTURE_PATH"]
