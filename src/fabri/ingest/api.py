"""``fabri.readlogs`` — the headline SDK ergonomic. Point it at any log; the
knowledge in it routes back into your own fabri agent's memory.

    import fabri
    summary = fabri.readlogs("prod/agent.jsonl", adapter="jsonl")
    print(summary.sessions, summary.by_kind, f"${summary.llm_cost_usd:.4f}")

Omit ``store``/``config`` and it resolves them from ``agent.yaml`` + the
configured memory backend — so an ingested guideline lands in the *same*
collection the agent retrieves from, with no extra wiring.
"""
from __future__ import annotations

from fabri.ingest.improver import Improver
from fabri.ingest.summary import IngestSummary


def readlogs(
    source,
    *,
    adapter: str = "auto",
    config=None,
    store=None,
    synthesize: bool = False,
    llm=None,
    adapter_options: dict | None = None,
    dry_run: bool = False,
) -> IngestSummary:
    """Normalize ``source`` via ``adapter`` and mine it into memory.

    ``source``: a file path, a directory, ``"-"``/stdin, or an iterable of
    lines or dict records.
    ``adapter``: a registered adapter name, an Adapter instance, or ``"auto"``.
    ``synthesize``: ``False`` (default) is deterministic and $0; ``True`` runs
    LLM guideline compression (needs a resolvable ``llm``/``config``).
    Returns an :class:`IngestSummary` (counts + cost; ``.entries`` for the
    mined ``MemoryEntry`` objects).
    """
    cfg = _resolve_config(config)
    if store is None:
        from fabri.runtime import build_memory_store

        store = build_memory_store(cfg["memory"])
    improver = Improver(store, cfg, synthesize=synthesize, llm=llm)
    return improver.ingest(source, adapter=adapter, adapter_options=adapter_options, dry_run=dry_run)


def _resolve_config(config) -> dict:
    from fabri.config import load_config

    if config is None or isinstance(config, str):
        return load_config(config)
    return config  # already a merged config dict
