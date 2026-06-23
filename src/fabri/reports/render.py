"""Format an AggregateReport as markdown / json / html.

Output is deliberately plain — no fancy tables, no fragile column alignment.
The markdown is meant to look right when pasted into a GitHub README, an X
post, or a deck.
"""
from __future__ import annotations

import json
from typing import Iterable

from fabri.reports.aggregate import AggregateReport, SessionSummary
from fabri.reports.chart import ascii_bars, ascii_sparkline, svg_trendline


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:.4f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _table(headers: list[str], rows: Iterable[list[str]]) -> str:
    """Markdown table, no width-padding (renders fine on GitHub)."""
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_markdown(report: AggregateReport, *, trend: bool = True) -> str:
    """Default `fabri report` output. The order is: headline numbers,
    then by-model, by-tool, outcomes, trend, then per-session detail (last)."""
    n = report.session_count
    parts = ["# fabri report", ""]

    # Headline
    parts.append(
        f"**{n} session{'s' if n != 1 else ''}**"
        + (
            f" · {report.priced_sessions} priced"
            if report.priced_sessions != n
            else ""
        )
        + f" · total COGS **{_fmt_usd(report.total_cost_usd)}**"
        + (
            f" (own {_fmt_usd(report.own_cost_usd)} + subagent {_fmt_usd(report.subagent_cost_usd)})"
            if report.subagent_cost_usd > 0
            else ""
        )
    )
    if report.tool_call_count:
        parts.append(
            f"**{report.tool_call_count} tool call{'s' if report.tool_call_count != 1 else ''}**"
            f" · {report.tool_failure_count} failed"
        )
    if report.avg_reuse_rate is not None:
        parts.append(
            f"**avg guideline reuse rate: {_fmt_pct(report.avg_reuse_rate)}**"
            " — what fraction of retrieved guidelines came from prior sessions"
        )
    parts.append("")

    # By model
    if report.cost_by_model:
        parts.append("## cost by model")
        parts.append("")
        parts.append(
            _table(
                ["model", "cost"],
                [[m, _fmt_usd(c)] for m, c in report.cost_by_model.items()],
            )
        )
        parts.append("")

    # By tool — G7
    if report.cost_by_tool:
        parts.append("## cost by tool (proportional)")
        parts.append("")
        parts.append(
            _table(
                ["tool", "cost", "share"],
                [
                    [
                        t,
                        _fmt_usd(c),
                        _fmt_pct(c / report.own_cost_usd) if report.own_cost_usd else "—",
                    ]
                    for t, c in report.cost_by_tool.items()
                ],
            )
        )
        parts.append("")
        parts.append(
            "_Proportional split of session cost across tool calls — see G7._"
        )
        parts.append("")

    # Outcomes
    if report.outcomes:
        parts.append("## outcomes")
        parts.append("")
        parts.append(
            _table(
                ["outcome", "count"],
                [[o, str(c)] for o, c in sorted(report.outcomes.items())],
            )
        )
        parts.append("")

    # Trend — G8
    if trend and report.sessions:
        # Oldest -> newest for the sparkline.
        ordered = list(reversed(report.sessions))
        costs = [s.cost_usd for s in ordered if s.cost_usd is not None]
        if len(costs) >= 2:
            spark = ascii_sparkline(costs, width=60)
            first, last = costs[0], costs[-1]
            delta = last - first
            pct = (delta / first * 100) if first else 0.0
            arrow = "↓" if delta < 0 else "↑" if delta > 0 else "→"
            parts.append("## cost per session (oldest → newest)")
            parts.append("")
            parts.append("```")
            parts.append(f"{_fmt_usd(min(costs))} {spark} {_fmt_usd(max(costs))}")
            parts.append(
                f"first → last: {_fmt_usd(first)} → {_fmt_usd(last)}  "
                f"({arrow}{abs(pct):.0f}%)"
            )
            parts.append("```")
            parts.append("")

    # Per-session detail (last so the headline reads cleanly)
    if report.sessions:
        parts.append("## sessions")
        parts.append("")
        rows = []
        for s in report.sessions:
            rows.append(
                [
                    s.started_iso or "—",
                    s.session_id[:8],
                    (s.task or "")[:60],
                    s.outcome or "—",
                    _fmt_usd(s.cost_usd),
                    str(s.step_count),
                    str(s.tool_call_count),
                ]
            )
        parts.append(
            _table(
                ["started", "session", "task", "outcome", "cost", "steps", "tools"],
                rows,
            )
        )
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_json(report: AggregateReport) -> str:
    """Machine-readable view. Lists each SessionSummary verbatim."""
    payload = {
        "session_count": report.session_count,
        "priced_sessions": report.priced_sessions,
        "total_cost_usd": report.total_cost_usd,
        "own_cost_usd": report.own_cost_usd,
        "subagent_cost_usd": report.subagent_cost_usd,
        "cost_by_model": report.cost_by_model,
        "cost_by_tool": report.cost_by_tool,
        "outcomes": report.outcomes,
        "tool_call_count": report.tool_call_count,
        "tool_failure_count": report.tool_failure_count,
        "avg_reuse_rate": report.avg_reuse_rate,
        "sessions": [
            {
                "session_id": s.session_id,
                "started_at": s.started_at,
                "task": s.task,
                "outcome": s.outcome,
                "cost_usd": s.cost_usd,
                "total_cost_usd": s.total_cost_usd,
                "subagent_cost_usd": s.subagent_cost_usd,
                "cost_by_model": s.cost_by_model,
                "cost_by_tool": s.cost_by_tool,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "step_count": s.step_count,
                "wall_time_s": s.wall_time_s,
                "tool_call_count": s.tool_call_count,
                "tool_failure_count": s.tool_failure_count,
                "guideline_reuse_rate": s.guideline_reuse_rate,
                "guidelines_retrieved": s.guidelines_retrieved,
                "guidelines_from_prior_sessions": s.guidelines_from_prior_sessions,
            }
            for s in report.sessions
        ],
    }
    return json.dumps(payload, indent=2)


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>fabri report</title>
<style>
  body { font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
         max-width: 980px; margin: 24px auto; padding: 0 16px; color: #1c1c1c; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 28px 0 8px; color: #555; font-weight: 600; }
  .headline { font-size: 16px; margin: 0 0 18px; color: #1c1c1c; }
  .headline b { color: #1f8b4c; }
  table { border-collapse: collapse; margin: 8px 0; font-size: 13px; }
  th, td { padding: 4px 12px 4px 0; text-align: left; vertical-align: top;
           border-bottom: 1px solid #eee; }
  th { color: #666; font-weight: 600; }
  .muted { color: #888; }
  .chart { margin: 6px 0 14px; }
  footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #eee;
           color: #888; font-size: 12px; }
</style>
</head>
<body>
"""

_HTML_FOOT = """
<footer>generated by <code>fabri report --html</code></footer>
</body>
</html>
"""


def _html_table(headers: list[str], rows: Iterable[list[str]]) -> str:
    out = ["<table>", "<thead><tr>"]
    out += [f"<th>{h}</th>" for h in headers]
    out.append("</tr></thead>")
    out.append("<tbody>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    out += ["</tbody>", "</table>"]
    return "".join(out)


def render_html(report: AggregateReport) -> str:
    """Self-contained HTML report (G20). No external CSS/JS, no fetches.
    Embeds the trendline as inline SVG."""
    n = report.session_count
    parts = [_HTML_HEAD, "<h1>fabri report</h1>"]
    parts.append(
        f"<p class='headline'>{n} session{'s' if n != 1 else ''} · "
        f"total COGS <b>{_fmt_usd(report.total_cost_usd)}</b>"
        + (
            f" (own {_fmt_usd(report.own_cost_usd)} + subagent {_fmt_usd(report.subagent_cost_usd)})"
            if report.subagent_cost_usd > 0
            else ""
        )
        + "</p>"
    )

    ordered = list(reversed(report.sessions))
    costs = [s.cost_usd for s in ordered if s.cost_usd is not None]
    if len(costs) >= 2:
        parts.append("<h2>cost per session (oldest → newest)</h2>")
        parts.append(
            "<div class='chart'>"
            + svg_trendline(costs, label="cost / session")
            + "</div>"
        )

    if report.cost_by_model:
        parts.append("<h2>cost by model</h2>")
        parts.append(
            _html_table(
                ["model", "cost"],
                [[m, _fmt_usd(c)] for m, c in report.cost_by_model.items()],
            )
        )

    if report.cost_by_tool:
        parts.append("<h2>cost by tool (proportional)</h2>")
        parts.append(
            _html_table(
                ["tool", "cost", "share"],
                [
                    [
                        t,
                        _fmt_usd(c),
                        _fmt_pct(c / report.own_cost_usd) if report.own_cost_usd else "—",
                    ]
                    for t, c in report.cost_by_tool.items()
                ],
            )
        )

    if report.outcomes:
        parts.append("<h2>outcomes</h2>")
        parts.append(
            _html_table(
                ["outcome", "count"],
                [[o, str(c)] for o, c in sorted(report.outcomes.items())],
            )
        )

    if report.sessions:
        parts.append("<h2>sessions</h2>")
        parts.append(
            _html_table(
                ["started", "session", "task", "outcome", "cost", "steps", "tools"],
                [
                    [
                        s.started_iso or "—",
                        s.session_id[:8],
                        (s.task or "")[:80],
                        s.outcome or "—",
                        _fmt_usd(s.cost_usd),
                        str(s.step_count),
                        str(s.tool_call_count),
                    ]
                    for s in report.sessions
                ],
            )
        )

    parts.append(_HTML_FOOT)
    return "".join(parts)
