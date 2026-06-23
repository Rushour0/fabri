"""`fabri report` machinery — aggregate JSONL traces into a usage report.

Reads `.fabri/traces/*.jsonl` (the homegrown observability spine; see
orchestrator/traces.py), groups events by session, and produces:

- a `SessionSummary` per trace (cost, tokens, outcome, by-model, by-tool)
- aggregate views (total / by-model / by-tool / outcome distribution / trend)
- renderers: markdown (default), json, html

The trace itself is the source of truth; nothing here mutates traces or memory.
"""
from fabri.reports.aggregate import (
    AggregateReport,
    SessionSummary,
    aggregate,
    collect_sessions,
)
from fabri.reports.chart import ascii_sparkline, svg_trendline
from fabri.reports.render import render_html, render_json, render_markdown

__all__ = [
    "AggregateReport",
    "SessionSummary",
    "aggregate",
    "collect_sessions",
    "ascii_sparkline",
    "svg_trendline",
    "render_html",
    "render_json",
    "render_markdown",
]
