"""Wilson score-interval helper for small-sample rubric rates.

BENCHMARKS.md reports rubric pass rates as small-sample k/N fractions (e.g.
"7/10 rubric criteria met"). A raw percentage from a handful of trials is
easy to over-read: 7/10 and 70/100 both print "70%" but carry very
different confidence. The Wilson score interval gives an honest confidence
bound on a binomial proportion that stays well-behaved at small N and at
the 0% / 100% extremes (unlike the naive normal approximation), so results
in BENCHMARKS.md can be reported as "70% (95% CI 40-89%)" instead of a bare
point estimate that overstates precision.
"""

from __future__ import annotations

import math


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Return the (low, high) Wilson score confidence bounds for k/n.

    ``k`` successes out of ``n`` trials, with confidence level implied by
    ``z`` (default 1.96 ~= 95%). Bounds are clamped to [0, 1]. Returns
    (0.0, 0.0) when n == 0 (no trials, no interval to report).
    """
    if n == 0:
        return (0.0, 0.0)

    p_hat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2 * n)
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z2 / (4 * n)) / n)

    low = (center - margin) / denom
    high = (center + margin) / denom

    low = min(1.0, max(0.0, low))
    high = min(1.0, max(0.0, high))
    return (low, high)


def fmt_rate(k: int, n: int) -> str:
    """Format a k/n rubric rate with its Wilson 95% CI, e.g.

    "7/10 (70%, 95% CI 40-89%)"
    """
    if n == 0:
        return f"{k}/{n} (n/a)"

    low, high = wilson_interval(k, n)
    pct = round(100 * k / n)
    low_pct = round(100 * low)
    high_pct = round(100 * high)
    return f"{k}/{n} ({pct}%, 95% CI {low_pct}-{high_pct}%)"
