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
    "First call read_file on notes/agenda.txt. If that exact file is unavailable, "
    "list its parent directory, verify the most plausible alternate file, and write "
    "a concise summary of every agenda item to notes/agenda_summary.txt."
)
HOLDOUT_TASK = (
    "Read notes/brief.txt, recover if needed, and write a concise summary of every "
    "brief item to notes/brief_summary.txt."
)
REQUIRED_SUMMARY_CONCEPTS = {
    "incident_ownership": ("incident", "follow-up", "owner|owns|ownership"),
    "release_notes": ("publish", "release", "notes"),
}
HOLDOUT_REQUIRED_SUMMARY_CONCEPTS = {
    "rollback_owner": ("rollback", "owner|owns|ownership|responsible"),
    "customer_update": ("customer", "update"),
}


def task_for_session(session: int) -> tuple[str, str]:
    """Return the training task first, then a semantically related holdout."""
    if session < 1:
        raise ValueError("session must be positive")
    return ("training", TASK) if session == 1 else ("holdout", HOLDOUT_TASK)


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


def grade_holdout_summary(workspace: Path) -> bool:
    """Return whether the holdout artifact captured both independent brief items."""
    summary = workspace / "notes" / "brief_summary.txt"
    if not summary.is_file():
        return False
    text = summary.read_text(encoding="utf-8").lower().replace("-", " ")
    return (
        "rollback" in text
        and any(token in text for token in ("owner", "owns", "ownership", "responsible"))
        and all(token in text for token in HOLDOUT_REQUIRED_SUMMARY_CONCEPTS["customer_update"])
    )


def _run_replica(args: tuple[int, str, str, str, int, float | None, str]) -> dict[str, Any]:
    replica, config_path, fixture_path, study_root, sessions, max_cost, condition = args
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
        task_label, task = task_for_session(session)
        if session == 2 and condition == "control":
            # Holdout control: preserve its fixture workspace but remove all
            # learned state, so it cannot retrieve the training lesson.
            (root / "memory.sqlite").unlink(missing_ok=True)
            shutil.rmtree(root / "state", ignore_errors=True)
            os.environ["FABRI_HOME"] = str(root / "state")
        output = workspace / "notes" / (
            "agenda_summary.txt" if task_label == "training" else "brief_summary.txt"
        )
        output.unlink(missing_ok=True)
        result = run_benchmark(task, str(rendered_config), runs=1).runs[0]
        row = result.__dict__.copy()
        row["session"] = session
        row["task_label"] = task_label
        row["summary_present"] = output.is_file()
        row["summary_text"] = (
            output.read_text(encoding="utf-8")[:1000] if output.is_file() else None
        )
        row["rubric_passed"] = (
            grade_summary(workspace) if task_label == "training" else grade_holdout_summary(workspace)
        )
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
    parser.add_argument(
        "--condition",
        choices=("memory", "control"),
        default="memory",
        help="Whether the holdout session retains training memory or starts fresh.",
    )
    args = parser.parse_args(argv)
    if args.replicas < 1 or args.sessions < 2 or args.workers < 1:
        parser.error("replicas/workers must be positive and sessions must be at least 2")

    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fabri-openai-study-") as temp:
        work = Path(temp)
        jobs = [
            (
                i, str(Path(args.config).resolve()), str(Path(args.fixture).resolve()),
                str(work), args.sessions, args.max_run_cost, args.condition,
            )
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
        "holdout_task": HOLDOUT_TASK,
        "condition": args.condition,
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
