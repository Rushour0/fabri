"""Offline retrieval eval — fast, deterministic, no API credits.

Loads a labeled fixture (a corpus of guidelines + queries tagged with the
guideline label(s) that should surface), seeds an in-process
`SqliteMemoryStore`, and runs each retrieval strategy through fabri's real
`_retrieve_inner` — the exact code path a live run uses. Reports recall@k /
MRR / precision@k per strategy.

This is the ground-truth gate that turns "retrieval is weak" from an anecdote
into a number, and makes every future retrieval change (reranker, query
expansion, default-strategy flip) measurable instead of vibes. No network and
no LLM: embeddings are the local MiniLM model, BM25 is SQLite FTS5.

See `docs/design/memory-observability-plan.md` (unit C). The pytest gate in
`tests/test_retrieval_eval_gate.py` asserts the dense baseline never regresses;
`python -m fabri.benchmarks.retrieval_eval` prints a head-to-head strategy table
(the before/after tool for the Track-M quality upgrades).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from fabri.benchmarks.retrieval_eval.metrics import (
    mean,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from fabri.memory.schema import MemoryEntry
from fabri.orchestrator.retrieval import RetrievalConfig, _retrieve_inner

# runner.py → retrieval_eval → benchmarks → fabri → src → repo root (parents[4]).
# The fixture lives under tests/ (repo-only, not shipped in the wheel); the
# --fixture flag lets a non-checkout caller point elsewhere.
FIXTURE_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "retrieval_eval.json"

DEFAULT_STRATEGIES = ("dense", "hybrid", "hybrid+mmr")
DEFAULT_TOP_K = 5
DEFAULT_KS = (1, 3, 5)


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    """Read and lightly validate the labeled fixture.

    Guards the two ground-truth-corrupting mistakes: a duplicate guideline text
    (two corpus rows would collapse to one store id under the deterministic id
    hash) and a query whose relevant label doesn't exist in the corpus."""
    data = json.loads(Path(path).read_text())
    corpus = data.get("corpus", [])
    queries = data.get("queries", [])
    if not corpus or not queries:
        raise ValueError(f"fixture {path} is missing a non-empty 'corpus' and 'queries'")

    texts = [c["text"] for c in corpus]
    if len(set(texts)) != len(texts):
        raise ValueError(
            "fixture corpus has duplicate guideline text; identical text hashes to "
            "the same store id and would collapse two rows into one (corrupting ground truth)"
        )
    labels = {c["label"] for c in corpus}
    if len(labels) != len(corpus):
        raise ValueError("fixture corpus has duplicate labels")
    for q in queries:
        missing = [r for r in q["relevant"] if r not in labels]
        if missing:
            raise ValueError(f"query {q['id']!r} references unknown corpus labels: {missing}")
        if not q["relevant"]:
            raise ValueError(f"query {q['id']!r} has an empty relevant set")
    return data


def seed_store(corpus: list[dict], db_path: Path):
    """Build a SqliteMemoryStore, upsert the corpus, and return
    (store, label_to_id). Asserts the store row count matches the corpus size so
    a silent id collision can't pass unnoticed."""
    # Lazy import so the module is importable (and the gate can `importorskip`)
    # even where sqlite-vec isn't installed.
    from fabri.memory.embedded_store import SqliteMemoryStore

    store = SqliteMemoryStore(path=db_path, collection="retrieval_eval")
    label_to_id: dict[str, str] = {}
    for row in corpus:
        entry = MemoryEntry(
            text=row["text"],
            kind=row["kind"],
            domain=row.get("domain", "generic"),
            tools=row.get("tools", []),
        )
        store.upsert(entry)
        label_to_id[row["label"]] = entry.id

    got = store.count()
    if got != len(corpus):
        raise RuntimeError(
            f"store has {got} rows after upserting {len(corpus)} corpus entries — "
            f"an id collision dropped rows; check for near-identical guideline text"
        )
    return store, label_to_id


def evaluate_strategy(
    store,
    queries: list[dict],
    label_to_id: dict[str, str],
    strategy: str,
    top_k: int,
    ks: tuple[int, ...],
) -> dict:
    """Run every query under one strategy and aggregate the IR metrics."""
    cfg = RetrievalConfig(strategy=strategy)
    per_recall: dict[int, list[float]] = {k: [] for k in ks}
    per_rr: list[float] = []
    per_prec: list[float] = []

    for q in queries:
        _text, merged = _retrieve_inner(store, q["task"], top_k=top_k, retrieval_config=cfg)
        retrieved = [entry.id for entry, _score in merged]
        relevant = {label_to_id[label] for label in q["relevant"]}
        for k in ks:
            per_recall[k].append(recall_at_k(retrieved, relevant, k))
        per_rr.append(reciprocal_rank(retrieved, relevant))
        per_prec.append(precision_at_k(retrieved, relevant, top_k))

    out = {f"recall@{k}": round(mean(per_recall[k]), 4) for k in ks}
    out["mrr"] = round(mean(per_rr), 4)
    out[f"precision@{top_k}"] = round(mean(per_prec), 4)
    return out


def run_eval(
    fixture_path: Path = FIXTURE_PATH,
    strategies: tuple[str, ...] | list[str] = DEFAULT_STRATEGIES,
    top_k: int = DEFAULT_TOP_K,
    ks: tuple[int, ...] = DEFAULT_KS,
    tmp_dir: Path | None = None,
) -> dict:
    """Seed one store from the fixture, then evaluate each strategy against it.

    Returns {strategy: {recall@k..., mrr, precision@k}}. The corpus is embedded
    once and reused across strategies (only the RetrievalConfig varies), so the
    whole sweep is a handful of vector queries per query row."""
    data = load_fixture(fixture_path)
    base = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.mkdtemp(prefix="fabri_retrieval_eval_"))
    base.mkdir(parents=True, exist_ok=True)
    store, label_to_id = seed_store(data["corpus"], base / "eval.db")

    results: dict[str, dict] = {}
    for strategy in strategies:
        results[strategy] = evaluate_strategy(
            store, data["queries"], label_to_id, strategy, top_k, ks
        )
    return results


def _format_table(results: dict, top_k: int, ks: tuple[int, ...]) -> str:
    cols = [f"recall@{k}" for k in ks] + ["mrr", f"precision@{top_k}"]
    header = "| strategy | " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---"] * (len(cols) + 1)) + "|"
    lines = [header, sep]
    for strategy, metrics in results.items():
        row = "| " + strategy + " | " + " | ".join(f"{metrics[c]:.4f}" for c in cols) + " |"
        lines.append(row)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fabri.benchmarks.retrieval_eval",
        description="Offline retrieval-quality eval (recall@k / MRR / precision@k) over a labeled fixture.",
    )
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH, help="path to the labeled fixture JSON")
    parser.add_argument(
        "--strategies",
        default=",".join(DEFAULT_STRATEGIES),
        help="comma-separated retrieval strategies (dense,sparse,hybrid,hybrid+mmr)",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="retrieval depth")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a markdown table")
    args = parser.parse_args(argv)

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    results = run_eval(
        fixture_path=args.fixture, strategies=strategies, top_k=args.top_k, ks=DEFAULT_KS
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        data = load_fixture(args.fixture)
        print(
            f"# fabri retrieval eval — {len(data['corpus'])} guidelines, "
            f"{len(data['queries'])} queries, top_k={args.top_k}\n"
        )
        print(_format_table(results, args.top_k, DEFAULT_KS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
