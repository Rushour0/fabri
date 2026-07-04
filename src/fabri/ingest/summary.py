"""``IngestSummary`` — what ``readlogs`` returns: counts + honest COGS. The
headline is the summary a host logs/dashboards; ``entries`` is exposed for the
one-line "route back into my agent" check."""
from __future__ import annotations

from dataclasses import dataclass, field

from fabri.core.llm import LLMUsage
from fabri.memory.schema import MemoryEntry
from fabri.pricing import cost_for


@dataclass
class IngestSummary:
    adapter: str = ""
    sessions: int = 0
    events: int = 0
    failures_mined: int = 0
    successes_mined: int = 0
    postmortems: int = 0
    skipped_lines: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    entries: list[MemoryEntry] = field(default_factory=list)
    llm_usage: LLMUsage = field(default_factory=LLMUsage)

    @property
    def llm_cost_usd(self) -> float:
        """Priced from accumulated usage; 0.0 on the deterministic ($0) path
        because no synthesis call ever ran."""
        return cost_for(self.llm_usage) or 0.0

    def add_entries(self, entries: list[MemoryEntry]) -> None:
        for e in entries:
            self.entries.append(e)
            self.by_kind[e.kind] = self.by_kind.get(e.kind, 0) + 1

    def accumulate_usage(self, u: LLMUsage) -> None:
        self.llm_usage.input_tokens += u.input_tokens
        self.llm_usage.output_tokens += u.output_tokens
        self.llm_usage.cache_creation_input_tokens += u.cache_creation_input_tokens
        self.llm_usage.cache_read_input_tokens += u.cache_read_input_tokens
        if u.model and not self.llm_usage.model:
            self.llm_usage.model = u.model

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "sessions": self.sessions,
            "events": self.events,
            "failures_mined": self.failures_mined,
            "successes_mined": self.successes_mined,
            "postmortems": self.postmortems,
            "skipped_lines": self.skipped_lines,
            "by_kind": self.by_kind,
            "entries": len(self.entries),
            "llm_cost_usd": round(self.llm_cost_usd, 6),
        }
