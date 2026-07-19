"""Run isolated, rubric-graded OpenAI memory-recovery benchmark replicas."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import shutil
import statistics
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from fabri.benchmarks.session_delta.runner import run_benchmark
from fabri.config import load_config

TASK = (
    "Read notes/agenda.txt, recover if needed, and write a concise summary of "
    "every agenda item to notes/agenda_summary.txt."
)
REQUIRED_SUMMARY_CONCEPTS = {
    "incident_ownership": ("incident", "follow-up", "owner|owns|ownership"),
    "release_notes": ("publish", "release", "notes"),
}


def grade_summary(workspace: Path) -> bool:
    """Return whether the newly written summary contains both agenda items."""
    summary = workspace / "notes" / "agenda_summary.txt"
    if not summary.is_file():
        return False
    text = summary.read_text(encoding="utf-8").lower().replace("-", " ")
    return (
        all(token in text for token in ("incident", "follow"))
        and any(token in text for token in ("owner", "owns", "ownership", "responsible"))
        and all(token in text for token in REQUIRED_SUMMARY_CONCEPTS["release_notes"])
    )


def _run_replica(args: tuple[int, str, str, str, int, float | None]) -> dict[str, Any]:
    replica, config_path, fixture_path, study_root, sessions, max_cost = args
    root = Path(study_root) / f"replica-{replica:02d}"
    workspace = root / "workspace"
    shutil.copytree(fixture_path, workspace)
    config = load_config(config_path)
    config["tools"]["sandbox_root"] = str(workspace)
    config["memory"]["sqlite_path"] = str(root / "memory.sqlite")
    config["agent"]["max_cost_usd"] = max_cost
    rendered_config = root / "agent.yaml"
    rendered_config.parent.mkdir(parents=True, exist_ok=True)
    rendered_config.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    import os

    os.environ["FABRI_HOME"] = str(root / "state")
    rows: list[dict[str, Any]] = []
    for session in range(1, sessions + 1):
        output = workspace / "notes" / "agenda_summary.txt"
        output.unlink(missing_ok=True)
        result = run_benchmark(TASK, str(rendered_config), runs=1).runs[0]
        row = result.__dict__.copy()
        row["session"] = session
        row["summary_present"] = output.is_file()
        row["summary_text"] = (
            output.read_text(encoding="utf-8")[:1000] if output.is_file() else None
        )
        row["rubric_passed"] = grade_summary(workspace)
        rows.append(row)
    return {"replica": replica, "runs": rows}


def summarize(replicas: list[dict[str, Any]]) -> dict[str, Any]:
    """Produce transparent aggregate metrics without hiding failed replicas."""
    first_costs = [r["runs"][0]["cost_usd"] for r in replicas if r["runs"][0]["cost_usd"] is not None]
    final_costs = [r["runs"][-1]["cost_usd"] for r in replicas if r["runs"][-1]["cost_usd"] is not None]
    rubric = [run["rubric_passed"] for r in replicas for run in r["runs"]]
    return {
        "replicas": len(replicas),
        "sessions_per_replica": len(replicas[0]["runs"]) if replicas else 0,
        "rubric_pass_rate": sum(rubric) / len(rubric) if rubric else None,
        "first_session_mean_cost_usd": statistics.mean(first_costs) if first_costs else None,
        "final_session_mean_cost_usd": statistics.mean(final_costs) if final_costs else None,
        "mean_cost_change_pct": (
            (statistics.mean(final_costs) - statistics.mean(first_costs))
            / statistics.mean(first_costs)
            * 100
            if first_costs and final_costs and statistics.mean(first_costs)
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--replicas", type=int, default=10)
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--max-run-cost", type=float, default=0.50)
    args = parser.parse_args(argv)
    if args.replicas < 1 or args.sessions < 2 or args.workers < 1:
        parser.error("replicas/workers must be positive and sessions must be at least 2")

    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fabri-openai-study-") as temp:
        work = Path(temp)
        jobs = [
            (i, str(Path(args.config).resolve()), str(Path(args.fixture).resolve()), str(work), args.sessions, args.max_run_cost)
            for i in range(1, args.replicas + 1)
        ]
        # Forking after the embedding stack is imported can terminate a child
        # during model initialization. Fresh spawned interpreters isolate it.
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers,
            mp_context=multiprocessing.get_context("spawn"),
        ) as pool:
            replicas = list(pool.map(_run_replica, jobs))

    payload = {
        "study": "openai-memory-recovery",
        "generated_at": datetime.now(UTC).isoformat(),
        "task": TASK,
        "rubric": {"required_summary_concepts": REQUIRED_SUMMARY_CONCEPTS},
        "config": str(Path(args.config)),
        "max_run_cost_usd": args.max_run_cost,
        "replicas": replicas,
        "summary": summarize(replicas),
    }
    (output / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
