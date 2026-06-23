"""Implementation of the session-N+1 cost delta benchmark.

Self-contained: imports run_agent + builds an LLM + memory store from a config,
then loops N times. Sub-agent costs roll up automatically (the agent loop
already aggregates `total_cost_usd`). No subprocess spawn — runs in-process so
profiling is straightforward.

Usage from the CLI:
    python -m fabri.benchmarks.session_delta --config agent.yaml \\
        --task "your fixed task" --runs 5
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fabri.config import load_config
from fabri.core.agent import run_agent
from fabri.core.logging_setup import configure_logging
from fabri.orchestrator.pipeline import process_trace
from fabri.paths import home
from fabri.reports.chart import ascii_sparkline
from fabri.runtime import (
    build_decompose_llm,
    build_llm,
    build_memory_store,
    build_tool_defs,
    build_tools,
)


@dataclass
class RunResult:
    session_id: str
    cost_usd: float | None
    total_cost_usd: float | None
    step_count: int
    wall_time_s: float
    outcome: str | None
    guideline_reuse_rate: float | None
    guidelines_retrieved: int
    guidelines_added: int  # tactical/strategic synthesized this run


@dataclass
class SessionDeltaResults:
    task: str
    runs: list[RunResult] = field(default_factory=list)

    @property
    def costs(self) -> list[float]:
        return [r.cost_usd for r in self.runs if r.cost_usd is not None]

    @property
    def first_cost(self) -> float | None:
        return self.costs[0] if self.costs else None

    @property
    def median_last3_cost(self) -> float | None:
        cs = self.costs
        if len(cs) < 3:
            return statistics.median(cs) if cs else None
        return statistics.median(cs[-3:])

    @property
    def delta_pct(self) -> float | None:
        first = self.first_cost
        last = self.median_last3_cost
        if first is None or last is None or first == 0:
            return None
        return round((last - first) / first * 100.0, 2)


def run_benchmark(
    task: str, config_path: str | None, runs: int = 5
) -> SessionDeltaResults:
    """Run the agent on `task` `runs` times. Returns aggregated results."""
    config = load_config(config_path)
    api_key_env = config["llm"]["api_key_env"]
    if not os.environ.get(api_key_env):
        raise RuntimeError(
            f"{api_key_env} is not set; export it before running the benchmark."
        )

    results = SessionDeltaResults(task=task)
    mem_cfg = config["memory"]
    tools_cfg = config["tools"]
    decompose_cfg = tools_cfg["decompose"]

    # Build the store once and reuse across runs — the whole point is for
    # guidelines from run N to be available to run N+1.
    store = build_memory_store(mem_cfg)
    tools = build_tools(tools_cfg)

    for i in range(runs):
        session_id = str(uuid.uuid4())
        configure_logging(session_id, verbose=False)
        llm = build_llm(config, build_tool_defs(tools, decompose_cfg))
        t0 = time.monotonic()
        result = run_agent(
            task,
            llm,
            tools,
            store,
            session_id=session_id,
            max_steps=config["agent"]["max_steps"],
            top_k=mem_cfg["top_k"],
            max_subquestions=decompose_cfg["max_subquestions"],
            system_prompt=config["agent"].get("system_prompt", ""),
            system_prompt_prefix=config["agent"].get("system_prompt_prefix", ""),
            result_format=tools_cfg.get("result_format", "toon"),
            output_format=config["agent"].get("output_format", "json"),
            decompose_llm=build_decompose_llm(config),
        )
        # Mine guidelines from the trace just like cmd_run does.
        compress_llm = build_llm(config, [])
        new_entries = process_trace(
            session_id,
            store,
            compress_llm,
            guideline_max_tokens=mem_cfg["guideline_max_tokens"],
            similarity_threshold=mem_cfg["similarity_threshold"],
            promotion_threshold_sessions=mem_cfg["promotion_threshold_sessions"],
        )
        usage = result.get("usage", {}) or {}
        rr = RunResult(
            session_id=session_id,
            cost_usd=usage.get("cost_usd"),
            total_cost_usd=usage.get("total_cost_usd"),
            step_count=usage.get("step_count", 0),
            wall_time_s=round(time.monotonic() - t0, 2),
            outcome=result.get("outcome"),
            guideline_reuse_rate=usage.get("guideline_reuse_rate"),
            guidelines_retrieved=usage.get("guidelines_retrieved", 0),
            guidelines_added=len(new_entries),
        )
        results.runs.append(rr)
        print(
            f"[run {i + 1}/{runs}] session={session_id[:8]} "
            f"cost=${(rr.cost_usd or 0):.4f} "
            f"steps={rr.step_count} "
            f"outcome={rr.outcome} "
            f"reuse={rr.guideline_reuse_rate} "
            f"+guidelines={rr.guidelines_added}",
            file=sys.stderr,
        )

    return results


def render_results_markdown(res: SessionDeltaResults) -> str:
    """Markdown summary suitable for pasting into the decks / a blog post."""
    n = len(res.runs)
    parts = [
        f"# session-N+1 cost delta benchmark",
        "",
        f"**Task:** {res.task}",
        f"**Runs:** {n}",
        "",
    ]
    costs = res.costs
    if costs:
        parts.append(f"first run: **${res.first_cost:.4f}**")
        parts.append(f"median of last 3: **${res.median_last3_cost:.4f}**")
        if res.delta_pct is not None:
            arrow = "↓" if res.delta_pct < 0 else "↑" if res.delta_pct > 0 else "→"
            parts.append(f"delta: **{arrow}{abs(res.delta_pct):.1f}%**")
        parts.append("")
        parts.append("trend (oldest → newest):")
        parts.append("```")
        parts.append(
            f"${min(costs):.4f} {ascii_sparkline(costs, width=60)} ${max(costs):.4f}"
        )
        parts.append("```")
        parts.append("")
    parts.append("| # | session | outcome | cost | steps | reuse | +guidelines |")
    parts.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(res.runs, start=1):
        cost_str = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "—"
        reuse = f"{(r.guideline_reuse_rate or 0) * 100:.0f}%" if r.guideline_reuse_rate is not None else "—"
        parts.append(
            f"| {i} | {r.session_id[:8]} | {r.outcome} | {cost_str} | "
            f"{r.step_count} | {reuse} | {r.guidelines_added} |"
        )
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fabri.benchmarks.session_delta")
    ap.add_argument("--config", default=None,
                    help="agent.yaml. Omitted = framework defaults.")
    ap.add_argument("--task", required=True, help="Fixed task to run N times")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--output-dir", default=None,
                    help="Where to write JSON + markdown results. "
                         "Defaults to $FABRI_HOME/.fabri/benchmarks/<ts>/.")
    args = ap.parse_args(argv)

    res = run_benchmark(args.task, args.config, runs=args.runs)

    out_dir = Path(args.output_dir) if args.output_dir else (
        home() / ".fabri" / "benchmarks" / str(int(time.time()))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "task": res.task,
                "first_cost": res.first_cost,
                "median_last3_cost": res.median_last3_cost,
                "delta_pct": res.delta_pct,
                "runs": [r.__dict__ for r in res.runs],
            },
            indent=2,
        )
    )
    md = render_results_markdown(res)
    (out_dir / "results.md").write_text(md)
    print(md)
    print(f"\nwrote {out_dir}/results.{{json,md}}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
