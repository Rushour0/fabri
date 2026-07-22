#!/usr/bin/env python3
"""Re-score completed study arms with their same-era rubric and report corrections.

The study records a rubric verdict at run time. When the scorer is later fixed (e.g. the
negation-aware forbidden window, or proximity matching for required terms), those recorded
verdicts become stale. This tool re-extracts each arm's actual holdout output from its trace
and re-scores it with the scorer as it exists now and the rubric from its own era, so a
run can be audited without re-spending.

Usage:
  python benchmarks/rescore_runs.py --run-root benchmarks/runs/<name> \
      --dataset benchmarks/datasets/company_memory_experiments.yaml [--case <case_id>]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from fabri.benchmarks.company_record import (
    CURRENT_RUBRIC_PROVENANCE,
    LEGACY_RUBRIC_PROVENANCE,
    score_arm_output,
)
from fabri.benchmarks.company_setup_probe import _as_required_groups, _as_string_list


def _final_text(arm_dir: Path, session_id: str) -> str | None:
    """Pull the FINAL event text for a holdout session out of its trace."""
    trace = arm_dir / "holdout-state" / ".fabri" / "traces" / f"{session_id}.jsonl"
    if not trace.exists():
        hits = list(arm_dir.rglob(f"{session_id}.jsonl"))
        if not hits:
            return None
        trace = hits[0]
    final = None
    for line in trace.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if str(event.get("type", "")).upper() == "FINAL":
            final = event
    if final is None:
        return None
    text = final.get("text") or final.get("result") or ""
    return text if isinstance(text, str) else json.dumps(text)


def _rubric_for_case(
    dataset: Path,
    case_id: str,
) -> tuple[
    tuple[tuple[str, ...], ...],
    dict[str, object],
    tuple[str, ...],
    tuple[tuple[str, ...], ...] | None,
    tuple[str, ...] | None,
]:
    data = yaml.safe_load(dataset.read_text())
    case = next(c for c in data["cases"] if c["id"] == case_id)
    expected = (case.get("holdout") or {}).get("expected") or case.get("expected") or {}
    structured = expected.get("structured", {})
    legacy_expected = case.get("legacy_expected")
    if legacy_expected is not None and not isinstance(legacy_expected, dict):
        raise ValueError(f"case {case_id}.legacy_expected must be a mapping")
    return (
        _as_required_groups(expected.get("required", []), "expected.required"),
        structured if isinstance(structured, dict) else {},
        _as_string_list(expected.get("forbidden", []), "expected.forbidden"),
        (
            _as_required_groups(
                legacy_expected.get("required", []),
                "legacy_expected.required",
            )
            if legacy_expected is not None
            else None
        ),
        (
            _as_string_list(
                legacy_expected.get("forbidden", []),
                "legacy_expected.forbidden",
            )
            if legacy_expected is not None
            else None
        ),
    )


def _discover_studies(root: Path, explicit_case_id: str | None) -> list[tuple[str, Path]]:
    """Find independently scored study roots under a run archive."""
    studies: list[tuple[str, Path]] = []
    seen: set[tuple[str, Path]] = set()
    for results_path in sorted(root.rglob("results.json")):
        try:
            metadata = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        case_id = metadata.get("case_id") if isinstance(metadata, dict) else None
        if not isinstance(case_id, str) or not case_id:
            continue
        if explicit_case_id is not None and case_id != explicit_case_id:
            continue
        study_root = results_path.parent
        if not any(study_root.rglob("private/result.json")):
            continue
        key = (case_id, study_root)
        if key not in seen:
            seen.add(key)
            studies.append(key)
    if studies:
        return studies
    if explicit_case_id is not None:
        return [(explicit_case_id, root)]
    raise SystemExit("could not infer --case; pass it explicitly")


def _score_rows(
    root: Path,
    required: tuple[tuple[str, ...], ...],
    structured: dict[str, object],
    forbidden: tuple[str, ...],
    legacy_required: tuple[tuple[str, ...], ...] | None,
    legacy_forbidden: tuple[str, ...] | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for result_path in sorted(root.rglob("private/result.json")):
        arm_dir = result_path.parent.parent
        rec = json.loads(result_path.read_text(encoding="utf-8"))
        sid = rec.get("holdout_session_id")
        text = _final_text(arm_dir, sid) if isinstance(sid, str) and sid else None
        raw_passed = rec.get("rubric_passed")
        scored = score_arm_output(
            rec,
            text,
            required,
            structured,
            forbidden,
            legacy_required_terms=legacy_required,
            legacy_forbidden_terms=legacy_forbidden,
        )
        scoring_mode = (
            str(scored["scoring_mode"])
            if scored is not None
            else (
                "structured"
                if "structured_output" in rec
                else "legacy_prose"
            )
        )
        rubric_provenance = (
            str(scored["rubric_provenance"])
            if scored is not None
            else (
                CURRENT_RUBRIC_PROVENANCE
                if "structured_output" in rec
                else LEGACY_RUBRIC_PROVENANCE
            )
        )
        if scored is None:
            rows.append({
                "replica": rec.get("replica"), "condition": rec.get("condition"),
                "raw_passed": raw_passed, "corrected_passed": None,
                "scoring_mode": scoring_mode,
                "rubric_provenance": rubric_provenance,
                "note": "no FINAL text found", "cost": rec.get("total_cost_usd"),
            })
            continue
        rows.append({
            "replica": rec.get("replica"), "condition": rec.get("condition"),
            "raw_passed": raw_passed,
            "raw_missing": rec.get("missing_required"), "raw_forbidden": rec.get("forbidden_hits"),
            "corrected_passed": bool(scored["passed"]),
            "corrected_missing": scored["missing"], "corrected_forbidden": scored["forbidden"],
            "scoring_mode": scoring_mode,
            "rubric_provenance": rubric_provenance,
            "cost": rec.get("total_cost_usd"),
            "guidelines_retrieved": rec.get("guidelines_retrieved"),
        })
    return rows


def _rate(
    rows: list[dict[str, object]],
    key: str,
) -> tuple[int, int, float | None]:
    values = [row[key] for row in rows if row.get(key) is not None]
    passed = sum(1 for value in values if value)
    rate = passed / len(values) * 100.0 if values else None
    return passed, len(values), rate


def _format_rate(passed: int, scored: int, rate: float | None) -> str:
    if rate is None:
        return "unavailable (0 scored)"
    return f"{rate:.1f}% ({passed}/{scored})"


def _print_case(case_id: str, rows: list[dict[str, object]]) -> None:
    print(f"case: {case_id}   arms: {len(rows)}")
    print(
        f"{'replica':>7} {'condition':<9} {'mode':<12} {'rubric':<16} "
        f"{'raw':>5} {'corrected':>10}  flipped  missing(raw -> corrected)"
    )
    for row in sorted(rows, key=lambda item: (item["condition"] or "", item["replica"] or 0)):
        flipped = "YES" if (
            row["raw_passed"] is not None
            and row["corrected_passed"] is not None
            and row["raw_passed"] != row["corrected_passed"]
        ) else ""
        print(
            f"{str(row['replica']):>7} {str(row['condition']):<9} "
            f"{str(row['scoring_mode']):<12} {str(row['rubric_provenance']):<16} "
            f"{str(row['raw_passed']):>5} "
            f"{str(row['corrected_passed']):>10}  {flipped:<7}  "
            f"{row.get('raw_missing')} -> {row.get('corrected_missing')}"
        )

    print("\n== pass rate by condition (raw -> corrected) ==")
    conditions = sorted(
        str(row["condition"])
        for row in rows
        if row.get("condition")
    )
    for condition in sorted(set(conditions)):
        subset = [row for row in rows if row["condition"] == condition]
        raw_passed, raw_scored, raw_rate = _rate(subset, "raw_passed")
        corrected_passed, corrected_scored, corrected_rate = _rate(
            subset,
            "corrected_passed",
        )
        costs = [
            float(row["cost"])
            for row in subset
            if isinstance(row.get("cost"), (int, float))
            and not isinstance(row.get("cost"), bool)
        ]
        mean_cost = f"${sum(costs) / len(costs):.4f}" if costs else "unavailable"
        print(
            f"  {condition:<9} arms={len(subset):<3} scored={corrected_scored:<3} "
            f"raw={_format_rate(raw_passed, raw_scored, raw_rate)}  "
            f"corrected={_format_rate(corrected_passed, corrected_scored, corrected_rate)}  "
            f"mean_cost={mean_cost}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--case", dest="case_id", default=None)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    root = Path(args.run_root)
    studies = _discover_studies(root, args.case_id)
    payloads: list[dict[str, object]] = []
    for index, (case_id, study_root) in enumerate(studies):
        (
            required,
            structured,
            forbidden,
            legacy_required,
            legacy_forbidden,
        ) = _rubric_for_case(Path(args.dataset), case_id)
        rows = _score_rows(
            study_root,
            required,
            structured,
            forbidden,
            legacy_required,
            legacy_forbidden,
        )
        if index:
            print()
        _print_case(case_id, rows)
        payloads.append({
            "case_id": case_id,
            "required": required,
            "structured": structured,
            "forbidden": forbidden,
            "legacy_expected": (
                {
                    "required": legacy_required,
                    "forbidden": legacy_forbidden,
                }
                if legacy_required is not None and legacy_forbidden is not None
                else None
            ),
            "rows": rows,
        })

    if args.json_out:
        payload: dict[str, object] = (
            payloads[0]
            if len(payloads) == 1
            else {"cases": payloads}
        )
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
