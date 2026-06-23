"""Unit tests for the `fabri report` machinery (G6/G7/G8/G20).

The reports module never touches Qdrant or any LLM — it only reads JSONL trace
files. So these tests build a synthetic trace under a temp dir, point
collect_sessions at it, and assert on the aggregated shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabri.reports import (
    aggregate,
    collect_sessions,
    render_html,
    render_json,
    render_markdown,
)
from fabri.reports.chart import ascii_bars, ascii_sparkline, svg_trendline


def _write_trace(traces_dir: Path, session_id: str, events: list[dict]) -> Path:
    """Write a JSONL trace to disk in the same shape orchestrator.traces does."""
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / f"{session_id}.jsonl"
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


def _make_session(
    session_id: str = "s1",
    ts: float = 1_700_000_000.0,
    task: str = "a task",
    outcome: str = "success",
    cost_usd: float | None = 0.012,
    cost_by_model: dict | None = None,
    tools: list[str] | None = None,
    reuse_rate: float | None = 0.5,
) -> list[dict]:
    """Build a minimal but realistic trace event sequence."""
    events: list[dict] = [
        {"ts": ts, "type": "start", "task": task, "context_block": ""},
        {"ts": ts + 0.1, "type": "step_started", "step": 1},
    ]
    for i, name in enumerate(tools or []):
        events.append({
            "ts": ts + 0.2 + i * 0.05,
            "type": "tool_call", "name": name,
            "args": {}, "result": {"ok": True, "result": "ok"},
        })
    events += [
        {"ts": ts + 0.5, "type": "step_finished", "step": 1, "reason": "final"},
        {"ts": ts + 0.6, "type": "final", "text": "done", "outcome": outcome},
    ]
    if cost_usd is not None:
        events.append({
            "ts": ts + 0.7, "type": "usage",
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "step_count": 1, "wall_time_s": 0.7,
            "cost_usd": cost_usd,
            "cost_by_model": cost_by_model or {"claude-sonnet-4-6": cost_usd},
            "subagent_cost_usd": 0.0,
            "total_cost_usd": cost_usd,
            "guideline_reuse_rate": reuse_rate,
            "guidelines_retrieved": 4 if reuse_rate is not None else 0,
            "guidelines_from_prior_sessions": int(round(4 * reuse_rate)) if reuse_rate is not None else 0,
        })
    return events


def test_collect_sessions_reads_traces_in_newest_first_order(tmp_path):
    """The CLI lists sessions newest first — the underlying collect must too,
    so `--limit N` always keeps the N most recent."""
    traces_dir = tmp_path / "traces"
    older = _write_trace(traces_dir, "older", _make_session("older", ts=100.0))
    newer = _write_trace(traces_dir, "newer", _make_session("newer", ts=200.0))
    # Force the mtime ordering (collect_sessions sorts by mtime, not by ts).
    import os
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))

    sessions = collect_sessions(traces_path=traces_dir)
    assert [s.session_id for s in sessions] == ["newer", "older"]


def test_session_summary_extracts_usage_and_outcome_from_trace(tmp_path):
    """The summary must pick up cost / outcome / tool counts / reuse rate from
    the trace shape that core.agent writes."""
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session(
        "s1", tools=["read_file", "read_file", "write_file"],
        reuse_rate=0.75,
    ))
    sessions = collect_sessions(traces_path=traces_dir)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.outcome == "success"
    assert s.cost_usd == 0.012
    assert s.tool_call_count == 3
    assert s.tool_counts == {"read_file": 2, "write_file": 1}
    assert s.guideline_reuse_rate == 0.75


def test_session_summary_attributes_cost_by_tool_proportionally(tmp_path):
    """G7: a session with cost C and tool calls split A:B should attribute
    C * A/(A+B) to the first tool and C * B/(A+B) to the second."""
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session(
        "s1", cost_usd=0.10,
        tools=["foo", "foo", "foo", "bar"],  # 3 foo + 1 bar => 75% / 25%
    ))
    sessions = collect_sessions(traces_path=traces_dir)
    s = sessions[0]
    assert s.cost_by_tool["foo"] == pytest.approx(0.075, rel=1e-3)
    assert s.cost_by_tool["bar"] == pytest.approx(0.025, rel=1e-3)


def test_session_summary_handles_legacy_traces_without_usage_event(tmp_path):
    """A trace from before v0.5.0 has no `usage` event. We must not crash —
    cost stays None (NOT 0.0, which would silently inflate aggregates)."""
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session("s1", cost_usd=None))
    sessions = collect_sessions(traces_path=traces_dir)
    assert sessions[0].cost_usd is None
    assert sessions[0].cost_by_tool == {}


def test_aggregate_sums_costs_and_counts_outcomes(tmp_path):
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session("s1", cost_usd=0.01, outcome="success"))
    _write_trace(traces_dir, "s2", _make_session("s2", cost_usd=0.02, outcome="success"))
    _write_trace(traces_dir, "s3", _make_session("s3", cost_usd=0.03, outcome="incomplete"))
    sessions = collect_sessions(traces_path=traces_dir)
    report = aggregate(sessions)
    assert report.session_count == 3
    assert report.priced_sessions == 3
    assert report.own_cost_usd == pytest.approx(0.06, rel=1e-3)
    assert report.outcomes == {"success": 2, "incomplete": 1}


def test_aggregate_avg_reuse_rate_excludes_none_sessions(tmp_path):
    """Sessions whose retrieval was empty have reuse_rate=None; they must not
    drag the average to 0."""
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session("s1", reuse_rate=0.5))
    _write_trace(traces_dir, "s2", _make_session("s2", reuse_rate=1.0))
    _write_trace(traces_dir, "s3", _make_session("s3", reuse_rate=None))
    report = aggregate(collect_sessions(traces_path=traces_dir))
    assert report.avg_reuse_rate == pytest.approx(0.75)


def test_render_markdown_includes_headline_and_per_session_table(tmp_path):
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session("s1"))
    report = aggregate(collect_sessions(traces_path=traces_dir))
    md = render_markdown(report)
    assert "# fabri report" in md
    assert "total COGS" in md
    assert "## sessions" in md


def test_render_json_round_trips(tmp_path):
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session("s1"))
    report = aggregate(collect_sessions(traces_path=traces_dir))
    payload = json.loads(render_json(report))
    assert payload["session_count"] == 1
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["sessions"][0]["cost_usd"] == 0.012


def test_render_html_is_self_contained(tmp_path):
    traces_dir = tmp_path / "traces"
    _write_trace(traces_dir, "s1", _make_session("s1"))
    _write_trace(traces_dir, "s2", _make_session("s2", cost_usd=0.008))
    report = aggregate(collect_sessions(traces_path=traces_dir))
    html = render_html(report)
    # No fragile fetches, no script tags — must be a single shippable file.
    assert "<html" in html
    assert "<style>" in html
    assert "<script" not in html
    assert "src=" not in html  # no external assets


# --- chart -----------------------------------------------------------------

def test_ascii_sparkline_empty_returns_empty():
    assert ascii_sparkline([]) == ""


def test_ascii_sparkline_flat_renders_uniform_band():
    """A flat sequence must still be visible — not collapse to nothing."""
    s = ascii_sparkline([0.5, 0.5, 0.5])
    assert len(s) == 3
    assert len(set(s)) == 1  # all identical chars


def test_ascii_sparkline_monotonic_descends_visibly():
    """Decreasing values should produce visible descent (last char < first)."""
    s = ascii_sparkline([1.0, 0.5, 0.25, 0.1])
    chars = " ▁▂▃▄▅▆▇█"
    assert chars.index(s[0]) > chars.index(s[-1])


def test_ascii_bars_empty_returns_empty():
    assert ascii_bars([]) == ""


def test_svg_trendline_is_valid_svg_with_polyline():
    svg = svg_trendline([0.01, 0.008, 0.006])
    assert svg.startswith("<svg")
    assert "<polyline" in svg
    assert svg.endswith("</svg>")
