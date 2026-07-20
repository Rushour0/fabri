"""``Improver`` — the object that turns any log into memory. It holds a store +
config and drives the loop: adapter → sessions → ``process_trace(events=...)`` →
dedup/promote into the same collection the host's agent retrieves from.

``readlogs`` (api.py) is the thin function wrapper; ``Improver`` is the reusable
form for a host that ingests many sources against one store.
"""
from __future__ import annotations

from typing import Iterable, Iterator

from fabri.core.llm import LLMBackend, LLMResponse
from fabri.core.logging_setup import get_logger
from fabri.events import EventType
from fabri.ingest.registry import register_adapter, resolve_adapter
from fabri.ingest.sources import LogSource, iter_sources
from fabri.ingest.summary import IngestSummary
from fabri.orchestrator.pipeline import process_trace

logger = get_logger()


class NoOpLLM:
    """Sentinel LLM for the deterministic ($0) path. If ``synthesize=False``
    ever tried to call the model, this raises — proving no billable call slips
    into the free path."""

    def step(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - guardrail
        raise RuntimeError(
            "NoOpLLM.step called: deterministic ingest must not invoke the LLM. "
            "Pass synthesize=True (and a real llm/config) to enable synthesis."
        )


class Improver:
    def __init__(
        self,
        store,
        config: dict | None = None,
        *,
        synthesize: bool = False,
        llm: LLMBackend | None = None,
        load_plugins: bool = True,
    ):
        self.store = store
        self.config = config or {}
        self.synthesize = synthesize
        self._llm = llm
        self._mem = (self.config.get("memory") or {}) if config else {}
        ingest_cfg = (self.config.get("ingest") or {}) if config else {}
        self.load_plugins = ingest_cfg.get("load_plugins", load_plugins)
        self.record_postmortems = ingest_cfg.get("record_postmortems", True)
        self._register_config_adapters(ingest_cfg)

    # -- construction ------------------------------------------------------
    @classmethod
    def from_config(cls, path: str | None = None, **kw) -> "Improver":
        from fabri.config import load_config
        from fabri.runtime import build_memory_store

        config = load_config(path)
        store = build_memory_store(config["memory"])
        return cls(store, config, **kw)

    def _register_config_adapters(self, ingest_cfg: dict) -> None:
        """Instantiate + register any adapters declared in ``ingest.adapters``
        (configmap / tool styles) so they're addressable by name."""
        for decl in ingest_cfg.get("adapters", []) or []:
            name = decl.get("name")
            kind = (decl.get("kind") or "configmap").lower()
            if not name:
                logger.warning("ingest: adapter decl missing 'name', skipping: %r", decl)
                continue
            if kind == "configmap":
                from fabri.ingest.adapters.configmap import ConfigMapAdapter

                register_adapter(name, ConfigMapAdapter(name, decl.get("mapping", {})))
            elif kind == "tool":
                from fabri.ingest.adapters.tool import ToolAdapter

                register_adapter(name, ToolAdapter.from_manifest_file(name, decl["manifest"]))
            else:
                logger.warning("ingest: unknown adapter kind %r for %r", kind, name)

    # -- the LLM used for synthesis (built lazily, only when needed) --------
    def _llm_for_run(self) -> LLMBackend:
        if not self.synthesize:
            return NoOpLLM()
        if self._llm is None:
            from fabri.runtime import build_llm

            self._llm = build_llm(self.config, [])
        return self._llm

    # -- ingestion ---------------------------------------------------------
    def ingest(self, source, *, adapter="auto", adapter_options=None, dry_run=False) -> IngestSummary:
        """Batch: mine every session across ``source`` (a file, dir, stdin, or
        iterator) into one summary."""
        summary = IngestSummary()
        for src in iter_sources(source):
            self._ingest_one(src, adapter, adapter_options or {}, dry_run, summary)
        return summary

    def ingest_stream(self, lines: Iterable, *, adapter, adapter_options=None, dry_run=False) -> Iterator[IngestSummary]:
        """Streaming: yield one ``IngestSummary`` per session as the adapter
        flushes it. Note: adapters that group by a session id can only flush a
        session once its group closes (end of input); a native/per-record
        adapter flushes incrementally."""
        src = LogSource.from_any(lines)
        adp = resolve_adapter(adapter, src, load_plugins=self.load_plugins)
        for session in adp.sessions(src, adapter_options or {}):
            per = IngestSummary(adapter=adp.name)
            self._mine_session(session, dry_run, per)
            per.skipped_lines = src.skipped
            yield per

    def _ingest_one(self, src: LogSource, adapter, options: dict, dry_run: bool, summary: IngestSummary) -> None:
        adp = resolve_adapter(adapter, src, load_plugins=self.load_plugins)
        summary.adapter = adp.name
        for session in adp.sessions(src, options):
            self._mine_session(session, dry_run, summary)
        summary.skipped_lines += src.skipped

    def _mine_session(self, session, dry_run: bool, summary: IngestSummary) -> None:
        from fabri.ingest.adapters.base import normalize_events

        events, skipped = normalize_events(session.events)
        summary.skipped_lines += skipped
        summary.events += len(events)
        summary.sessions += 1
        if not events:
            return
        failures = sum(
            1 for e in events
            if e.get("type") == EventType.TOOL_CALL.value and not (e.get("result") or {}).get("ok")
        )
        summary.failures_mined += failures
        if dry_run:
            return
        entries = process_trace(
            session.session_id,
            self.store,
            self._llm_for_run(),
            guideline_max_tokens=self._mem.get("guideline_max_tokens", 30),
            similarity_threshold=self._mem.get("similarity_threshold", 0.85),
            promotion_threshold_sessions=self._mem.get("promotion_threshold_sessions", 3),
            record_postmortem=self.record_postmortems,
            success_pattern_requires_evidence=self._mem.get("success_pattern_requires_evidence", False),
            on_usage=summary.accumulate_usage,
            events=events,
            synthesize=self.synthesize,
        )
        summary.add_entries(entries)
        summary.postmortems = summary.by_kind.get("postmortem", 0)
        summary.successes_mined = summary.by_kind.get("success_pattern", 0)

    def close(self) -> None:
        """Release any adapter-owned resources (sandbox subprocess pools, etc.).
        No-op today; present so hosts can use ``Improver`` as a context object."""
        pass

    def __enter__(self) -> "Improver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
