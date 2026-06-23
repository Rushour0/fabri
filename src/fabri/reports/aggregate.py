"""Read every JSONL trace under `traces_dir()` and roll up cost / outcome /
by-model / by-tool stats.

Cost-by-tool (G7) is a proportional split of the session's own `cost_usd` over
the tool_call events in the session — crude but actionable. A future version
will do per-step attribution (each step's LLM cost split across the tools it
dispatched that turn); the proportional split is a good-enough first cut and
costs nothing extra at trace time.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fabri.core.logging_setup import get_logger
from fabri.paths import traces_dir

_logger = get_logger()


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL trace from a specific path (collect_sessions accepts a
    custom traces directory, so we can't go through orchestrator.traces.read_trace
    which is hard-wired to the FABRI_HOME-derived default location). One
    malformed line is logged + skipped, not fatal."""
    if not path.exists():
        return []
    out: list[dict] = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            _logger.warning("trace %s: skipping malformed line %d: %s", path.name, i, e)
    return out


@dataclass
class SessionSummary:
    """One row per session — the unit `fabri report` aggregates over."""

    session_id: str
    task: str = ""
    started_at: float = 0.0
    outcome: str | None = None

    # Tokens
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    # Cost — None means "no usage event emitted" (this session predates the
    # COGS instrumentation in v0.6.0). 0.0 means "priced but actually free".
    cost_usd: float | None = None
    total_cost_usd: float | None = None
    subagent_cost_usd: float | None = None
    cost_by_model: dict[str, float] = field(default_factory=dict)
    cost_by_tool: dict[str, float] = field(default_factory=dict)

    # Activity
    step_count: int = 0
    wall_time_s: float | None = None
    tool_call_count: int = 0
    tool_failure_count: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)

    # G4: guideline reuse rate — % of retrieved guidelines that came from
    # *prior* sessions (not added by this run). None if no retrievals.
    guideline_reuse_rate: float | None = None
    guidelines_retrieved: int = 0
    guidelines_from_prior_sessions: int = 0

    @property
    def started_iso(self) -> str:
        if not self.started_at:
            return ""
        return _dt.datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")


def _attribute_cost_by_tool_proportional(
    cost_usd: float | None, tool_counts: dict[str, int]
) -> dict[str, float]:
    """v0.7.0 proportional fallback. Kept for sessions whose trace doesn't
    carry per-step cost (older runs, scripted backends with no priced model).
    """
    if cost_usd is None or not tool_counts:
        return {}
    total_calls = sum(tool_counts.values())
    if total_calls == 0:
        return {}
    return {
        name: round(cost_usd * count / total_calls, 6)
        for name, count in tool_counts.items()
    }


def _attribute_cost_by_tool_per_step(events: list[dict]) -> dict[str, float] | None:
    """G7 per-step attribution: walk the trace, keep a step → list-of-tool-names
    map and a step → cost_usd map; the cost of step N gets split equally across
    the tools dispatched at step N.

    This is more accurate than proportional-by-total: a single read_file in a
    cheap-context step doesn't inherit cost from an expensive code-generation
    step.

    Returns None when no `step_finished` event carries a `cost_usd` field
    (legacy traces) so the caller can fall back to the proportional split.
    """
    step_tools: dict[int, list[str]] = {}
    step_cost: dict[int, float] = {}
    saw_any_cost = False
    for ev in events:
        kind = ev.get("type")
        if kind == "tool_call":
            step = ev.get("step")
            if step is None:
                continue
            step_tools.setdefault(step, []).append(ev.get("name", "?"))
        elif kind == "step_finished":
            c = ev.get("cost_usd")
            if c is not None:
                saw_any_cost = True
                step_cost[ev.get("step", -1)] = float(c)
    if not saw_any_cost:
        return None
    out: dict[str, float] = {}
    for step, tools in step_tools.items():
        cost = step_cost.get(step, 0.0)
        if not tools or cost == 0.0:
            continue
        share = cost / len(tools)
        for name in tools:
            out[name] = out.get(name, 0.0) + share
    return {k: round(v, 6) for k, v in out.items()}


def summarize_session(events: list[dict], session_id: str) -> SessionSummary:
    """Roll up one session's events into a SessionSummary. Tolerant of legacy
    traces missing the `usage` event (no `cost_usd` recorded)."""
    summary = SessionSummary(session_id=session_id)

    if not events:
        return summary

    summary.started_at = events[0].get("ts", 0.0)

    for ev in events:
        kind = ev.get("type")
        if kind == "start":
            summary.task = ev.get("task", "")
        elif kind == "tool_call":
            summary.tool_call_count += 1
            name = ev.get("name", "?")
            summary.tool_counts[name] = summary.tool_counts.get(name, 0) + 1
            result = ev.get("result", {}) or {}
            if result.get("ok") is False:
                summary.tool_failure_count += 1
        elif kind in ("final", "failed", "incomplete"):
            summary.outcome = ev.get("outcome", kind)
        elif kind == "usage":
            summary.input_tokens = ev.get("input_tokens", 0)
            summary.output_tokens = ev.get("output_tokens", 0)
            summary.cache_creation_input_tokens = ev.get("cache_creation_input_tokens", 0)
            summary.cache_read_input_tokens = ev.get("cache_read_input_tokens", 0)
            summary.cost_usd = ev.get("cost_usd")
            summary.total_cost_usd = ev.get("total_cost_usd")
            summary.subagent_cost_usd = ev.get("subagent_cost_usd")
            summary.cost_by_model = dict(ev.get("cost_by_model", {}) or {})
            summary.step_count = ev.get("step_count", 0)
            summary.wall_time_s = ev.get("wall_time_s")
            # G4: guideline reuse fields are optional — older traces omit them.
            summary.guideline_reuse_rate = ev.get("guideline_reuse_rate")
            summary.guidelines_retrieved = ev.get("guidelines_retrieved", 0)
            summary.guidelines_from_prior_sessions = ev.get(
                "guidelines_from_prior_sessions", 0
            )

    # G7: prefer per-step attribution from the trace itself; fall back to the
    # v0.7.0 proportional split for legacy traces with no per-step cost field.
    per_step = _attribute_cost_by_tool_per_step(events)
    if per_step is not None:
        summary.cost_by_tool = per_step
    else:
        summary.cost_by_tool = _attribute_cost_by_tool_proportional(
            summary.cost_usd, summary.tool_counts
        )
    return summary


def collect_sessions(
    traces_path: Path | None = None,
    *,
    since_seconds: float | None = None,
    limit: int | None = None,
) -> list[SessionSummary]:
    """Walk traces_dir, return SessionSummary per file, newest first.

    `since_seconds`: keep only sessions whose mtime is within this many seconds
    of now (e.g. 7*86400 for last week). None = no time filter.
    `limit`: keep at most this many (after time filter, after sort).
    """
    d = traces_path if traces_path is not None else traces_dir()
    if not d.exists():
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if since_seconds is not None:
        cutoff = time.time() - since_seconds
        files = [p for p in files if p.stat().st_mtime >= cutoff]
    if limit is not None:
        files = files[:limit]
    sessions = []
    for p in files:
        events = _read_jsonl(p)
        sessions.append(summarize_session(events, p.stem))
    return sessions


@dataclass
class AggregateReport:
    """Rolled-up view across many sessions — what `fabri report` prints."""

    sessions: list[SessionSummary]
    total_cost_usd: float = 0.0
    own_cost_usd: float = 0.0
    subagent_cost_usd: float = 0.0
    cost_by_model: dict[str, float] = field(default_factory=dict)
    cost_by_tool: dict[str, float] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    tool_failure_count: int = 0
    tool_call_count: int = 0
    priced_sessions: int = 0  # sessions that had a usage event
    avg_reuse_rate: float | None = None

    @property
    def session_count(self) -> int:
        return len(self.sessions)


def aggregate(sessions: list[SessionSummary]) -> AggregateReport:
    """Roll a list of SessionSummary into one AggregateReport."""
    report = AggregateReport(sessions=sessions)
    by_model = Counter()
    by_tool = Counter()
    outcomes = Counter()
    reuse_rates = []

    for s in sessions:
        if s.outcome:
            outcomes[s.outcome] += 1
        report.tool_call_count += s.tool_call_count
        report.tool_failure_count += s.tool_failure_count
        if s.cost_usd is not None:
            report.priced_sessions += 1
            report.own_cost_usd += s.cost_usd
        if s.subagent_cost_usd is not None:
            report.subagent_cost_usd += s.subagent_cost_usd
        if s.total_cost_usd is not None:
            report.total_cost_usd += s.total_cost_usd
        for model, c in s.cost_by_model.items():
            by_model[model] += c
        for tool, c in s.cost_by_tool.items():
            by_tool[tool] += c
        if s.guideline_reuse_rate is not None:
            reuse_rates.append(s.guideline_reuse_rate)

    report.cost_by_model = {m: round(c, 6) for m, c in by_model.most_common()}
    report.cost_by_tool = {t: round(c, 6) for t, c in by_tool.most_common()}
    report.outcomes = dict(outcomes)
    report.own_cost_usd = round(report.own_cost_usd, 6)
    report.subagent_cost_usd = round(report.subagent_cost_usd, 6)
    report.total_cost_usd = round(report.total_cost_usd, 6)
    if reuse_rates:
        report.avg_reuse_rate = round(sum(reuse_rates) / len(reuse_rates), 4)
    return report
