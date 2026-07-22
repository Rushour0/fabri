"""Hermetic builders for benchmark run trees used by unit tests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

CASE_ID = "benchmark_case"
PASSING_TEXT = "Checkout was rolled back. A follow-up will be provided."

_DEFAULT = object()


def write_dataset(
    directory: Path,
    *,
    case_id: str = CASE_ID,
    required: Sequence[object] = (("checkout",), ("rollback", "rolled back"), ("follow-up",)),
    forbidden: Sequence[str] = ("blame",),
) -> Path:
    """Write a one-case dataset with the production rubric shape."""
    path = directory / "dataset.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "cases": [
                    {
                        "id": case_id,
                        "expected": {
                            "required": list(required),
                            "forbidden": list(forbidden),
                        },
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def write_run(
    directory: Path,
    *,
    name: str = "run",
    case_id: str = CASE_ID,
    include_results: bool = True,
) -> Path:
    """Create a run root and its public study metadata."""
    run_root = directory / name
    run_root.mkdir(parents=True)
    if include_results:
        (run_root / "results.json").write_text(
            json.dumps(
                {
                    "company": "fixture-company",
                    "case_id": case_id,
                    "generated_at": "2026-07-22T00:00:00+00:00",
                    "fabri_version": "test-version",
                    "replicas": 1,
                }
            ),
            encoding="utf-8",
        )
    return run_root


def write_arm(
    run_root: Path,
    *,
    replica: int,
    condition: str,
    arm_name: str | None = None,
    holdout_complete: bool = True,
    rubric_passed: bool | None = True,
    training_failure_reasons: Sequence[str] = (),
    total_cost_usd: object = _DEFAULT,
    guidelines_retrieved: int | None = 0,
    missing_required: Sequence[str] = (),
    forbidden_hits: Sequence[str] = (),
    session_id: str | None = None,
    trace_events: Sequence[Mapping[str, object] | str] | None | object = _DEFAULT,
    trace_subdir: Path = Path("holdout-state/.fabri/traces"),
) -> Path:
    """Write one arm result and, unless disabled, its holdout trace."""
    name = arm_name or f"{condition}-{replica}"
    sid = session_id or f"{name}-session"
    arm_dir = run_root / name
    result_dir = arm_dir / "private"
    result_dir.mkdir(parents=True)
    cost = 1.0 if total_cost_usd is _DEFAULT else total_cost_usd
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "replica": replica,
                "condition": condition,
                "holdout_complete": holdout_complete,
                "rubric_passed": rubric_passed,
                "training_failure_reasons": list(training_failure_reasons),
                "total_cost_usd": cost,
                "guidelines_retrieved": guidelines_retrieved,
                "holdout_session_id": sid,
                "missing_required": list(missing_required),
                "forbidden_hits": list(forbidden_hits),
            }
        ),
        encoding="utf-8",
    )

    events = (
        [{"type": "FINAL", "text": PASSING_TEXT}]
        if trace_events is _DEFAULT
        else trace_events
    )
    if events is not None:
        trace_dir = arm_dir / trace_subdir
        trace_dir.mkdir(parents=True)
        lines = [event if isinstance(event, str) else json.dumps(event) for event in events]
        (trace_dir / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return arm_dir
