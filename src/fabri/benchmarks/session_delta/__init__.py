"""session-N+1 cost delta benchmark — fabri's own metric.

Hypothesis: re-running the same task N times, with the memory loop active,
should produce a downward trend in `cost_usd` after the first few runs, as
recurring failure modes get compressed into guidelines and re-injected on
later runs.

How:
1. Run the agent on a fixed task N times, each with a fresh session_id.
2. Between runs, the trace gets mined into guidelines (the standard pipeline).
3. Record per-run cost / outcome / step count / reuse rate.
4. Emit (a) a JSON results file, (b) a markdown summary chart, (c) the delta:
   first_run_cost vs median_of_last_3 cost.

The result is what goes on slide 4 of the sales deck and slide 9 of the
technical deck — see `decks/`.
"""
from fabri.benchmarks.session_delta.runner import (
    RunResult,
    SessionDeltaResults,
    run_benchmark,
)

__all__ = ["RunResult", "SessionDeltaResults", "run_benchmark"]
