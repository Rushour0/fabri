"""ASCII sparklines + minimal SVG trendlines — zero-dep, no matplotlib.

ASCII goes in the terminal (the COGS chart you screenshot for X). SVG goes
in the HTML report (G20) and pastes into decks/blogs. Both render the same
data shape: a list of floats (oldest -> newest), one per session/bucket.
"""
from __future__ import annotations

import html

# Unicode block elements, lowest to highest.
_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def ascii_sparkline(values: list[float], width: int | None = None) -> str:
    """Render a sequence of values as a unicode sparkline. Empty/zero values
    render as a single space (the lowest band) so a long flat zero stretch
    still has visible width and doesn't collapse. `width` truncates from the
    left (keeps the most recent) when set."""
    if not values:
        return ""
    if width is not None and len(values) > width:
        values = values[-width:]
    lo = min(values)
    hi = max(values)
    span = hi - lo
    if span == 0:
        # Everything identical — middle band gives a visible flat line.
        return _SPARK_CHARS[len(_SPARK_CHARS) // 2] * len(values)
    out = []
    for v in values:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        idx = max(0, min(len(_SPARK_CHARS) - 1, idx))
        out.append(_SPARK_CHARS[idx])
    return "".join(out)


def ascii_bars(values: list[float], width: int = 40) -> str:
    """A horizontal bar per value — used for the per-week trend block in
    fabri report's markdown output. Each bar is at most `width` cells of `█`.
    """
    if not values:
        return ""
    hi = max(values) or 1.0
    lines = []
    for v in values:
        n = int(round((v / hi) * width)) if v > 0 else 0
        lines.append("█" * max(0, n) + " " * max(0, width - n))
    return "\n".join(lines)


def svg_trendline(
    values: list[float],
    *,
    width: int = 600,
    height: int = 160,
    padding: int = 24,
    stroke: str = "#1f8b4c",
    fill: str | None = "rgba(31,139,76,0.10)",
    label: str | None = None,
) -> str:
    """Render the same value sequence as an inline SVG suitable for embedding
    in HTML or a blog/X post. Self-contained — no external CSS/JS."""
    if not values:
        return f"<svg width='{width}' height='{height}' xmlns='http://www.w3.org/2000/svg'></svg>"
    n = len(values)
    lo = min(values)
    hi = max(values)
    span = hi - lo or 1.0
    inner_w = width - 2 * padding
    inner_h = height - 2 * padding

    def x(i):
        if n == 1:
            return padding + inner_w / 2
        return padding + (i * inner_w / (n - 1))

    def y(v):
        return padding + inner_h - ((v - lo) / span * inner_h)

    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    parts = [
        f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' "
        "xmlns='http://www.w3.org/2000/svg' font-family='ui-monospace, monospace' "
        "font-size='12'>",
        f"<rect width='{width}' height='{height}' fill='white'/>",
    ]
    if fill:
        fill_pts = (
            f"{padding},{padding + inner_h} "
            + points
            + f" {padding + inner_w},{padding + inner_h}"
        )
        parts.append(f"<polygon points='{fill_pts}' fill='{fill}' stroke='none'/>")
    parts.append(
        f"<polyline points='{points}' fill='none' stroke='{stroke}' stroke-width='2'/>"
    )
    # Axis labels — only the high and low bounds, kept minimal.
    parts.append(
        f"<text x='{padding}' y='{padding - 6}' fill='#666'>${hi:.4f}</text>"
    )
    parts.append(
        f"<text x='{padding}' y='{height - 6}' fill='#666'>${lo:.4f}</text>"
    )
    if label:
        # Escape in case a caller ever passes trace-derived text as the label.
        parts.append(
            f"<text x='{width - padding}' y='{padding - 6}' text-anchor='end' "
            f"fill='#444' font-weight='600'>{html.escape(str(label))}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)
