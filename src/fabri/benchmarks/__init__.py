"""fabri benchmarks — the harness that turns the memory-loop claim into a number.

Without published numbers, the pitch is anecdotes. This module ships:

- `session_delta` — fabri's own metric: run an agent on the same task N times,
  record cost + outcome each time, measure whether cost drops as the memory
  loop adds guidelines. The single sharpest test of "does the loop work?"
- `longmemeval` — a planned port of the public LongMemEval benchmark for
  apples-to-apples comparison against Mastra / Letta / Mem0 / Zep. Scaffold
  in place; dataset port is a follow-up (`benchmarks/longmemeval/README.md`).

CLI:
    python -m fabri.benchmarks.session_delta --config agent.yaml \\
        --task "list every README in this repo" --runs 5

Output: a markdown report + JSON results under `.fabri/benchmarks/<run_id>/`.
"""
