"""Pure information-retrieval metrics for the offline retrieval eval.

No I/O, no embeddings, no external deps — every function operates on a ranked
list of ids plus a set of relevant ids, so the module stays trivially
unit-testable and fully deterministic. Higher is better for every metric and
all outputs are in [0, 1].

These back `runner.py`, which turns fabri's real `_retrieve_inner` output into
`(retrieved_ids, relevant_ids)` pairs. Keeping the math here — separate from any
store or embedding — is what lets the eval gate run in CI in milliseconds once
the corpus is embedded.
"""
from __future__ import annotations

from collections.abc import Sequence


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top-k retrieved ids.

    Raises on an empty relevant set — a query with no gold answer can't have a
    recall, and silently returning 0.0 or 1.0 would poison the aggregate. The
    caller (a fixture) is expected to guarantee every query has ≥1 relevant id.
    """
    if not relevant:
        raise ValueError("recall_at_k: relevant set is empty (query has no gold answer)")
    if k <= 0:
        raise ValueError(f"recall_at_k: k must be positive, got {k}")
    top = retrieved[:k]
    hits = sum(1 for r in top if r in relevant)
    return hits / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-k retrieved ids that are relevant.

    Denominator is the number of ids actually returned in the top-k (`len(top)`),
    not `k` — so a strategy that legitimately returns fewer than k candidates
    isn't unfairly penalised for the empty slots. Returns 0.0 when nothing is
    retrieved.
    """
    if k <= 0:
        raise ValueError(f"precision_at_k: k must be positive, got {k}")
    top = retrieved[:k]
    if not top:
        return 0.0
    hits = sum(1 for r in top if r in relevant)
    return hits / len(top)


def reciprocal_rank(retrieved: Sequence[str], relevant: set[str]) -> float:
    """1 / (1-indexed rank of the first relevant id), or 0.0 if none appear.

    The per-query term of MRR. Rewards surfacing a correct guideline early —
    the property that matters when only the top few entries make it into the
    prompt's token budget.
    """
    for rank, rid in enumerate(retrieved, start=1):
        if rid in relevant:
            return 1.0 / rank
    return 0.0


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean; 0.0 for an empty sequence (no queries → no signal)."""
    values = list(values)
    return sum(values) / len(values) if values else 0.0
