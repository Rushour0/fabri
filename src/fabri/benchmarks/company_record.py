"""Normalize a completed company study into ONE tracked benchmark record.

A study run is a directory of per-arm results. For tracking a company over time we
want a single, stable, machine-readable row per (company, date, benchmark) that can be
appended to a history file and rendered on the catalog site.

Key properties this record captures that a raw pass-rate hides:
  * raw vs corrected quality (the recorded verdict vs a same-era scorer re-run)
  * PAIRED cost deltas with a sign test -- means over unequal arms are misleading
  * which arms were EXCLUDED and why (training timeout / incomplete), never silently averaged
  * whether the no-memory control was genuinely memory-free (0 guidelines retrieved)

Usage:
  python -m fabri.benchmarks.company_record --run-root benchmarks/runs/<name>/<company> \
      --case <case_id> --dataset benchmarks/datasets/company_memory_experiments.yaml
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Mapping
from math import comb
from pathlib import Path

import yaml

from fabri.benchmarks.company_setup_probe import (
    _as_required_groups,
    _as_string_list,
    score_structured,
    score_text,
)


CURRENT_RUBRIC_PROVENANCE = "expected"
LEGACY_RUBRIC_PROVENANCE = "legacy_expected"


def score_arm_output(
    record: Mapping[str, object],
    final_text: str | None,
    required_terms: tuple[tuple[str, ...], ...],
    structured_fields: Mapping[str, object],
    forbidden_terms: tuple[str, ...],
    *,
    prose_field: str = "response",
    legacy_required_terms: tuple[tuple[str, ...], ...] | None = None,
    legacy_forbidden_terms: tuple[str, ...] | None = None,
) -> dict[str, object] | None:
    """Score one arm while preserving the structured/legacy archive boundary.

    A present ``structured_output`` key always selects structured scoring, even
    when its value is null or malformed. Only a genuinely absent key may use
    the legacy prose-required rubric.
    """
    if "structured_output" not in record:
        if legacy_required_terms is None or legacy_forbidden_terms is None:
            raise ValueError(
                "legacy_expected is required for legacy_prose scoring; "
                "refusing to borrow the current expected rubric"
            )
        if final_text is None:
            return None
        legacy = score_text(
            final_text,
            legacy_required_terms,
            legacy_forbidden_terms,
        )
        return {
            "passed": bool(legacy["passed"]),
            "missing": legacy["missing"],
            "forbidden": legacy["forbidden"],
            "scoring_mode": "legacy_prose",
            "rubric_provenance": LEGACY_RUBRIC_PROVENANCE,
        }

    structured_output = record["structured_output"]
    if structured_fields:
        required = score_structured(structured_output, structured_fields)
    else:
        required = {
            "passed": False,
            "mismatches": ["expected.structured:not_configured"],
        }
    prose = (
        structured_output.get(prose_field)
        if isinstance(structured_output, dict)
        else None
    )
    mismatches = list(required["mismatches"])
    if isinstance(structured_output, dict):
        if prose_field not in structured_output:
            mismatches.append(f"missing:{prose_field}")
        elif not isinstance(prose, str):
            mismatches.append(f"wrong:{prose_field}")
    safety = score_text(
        prose if isinstance(prose, str) else "",
        (),
        forbidden_terms,
    )
    return {
        "passed": bool(not mismatches and safety["passed"]),
        "missing": mismatches,
        "forbidden": safety["forbidden"],
        "scoring_mode": "structured",
        "rubric_provenance": CURRENT_RUBRIC_PROVENANCE,
    }


def _sign_test_p(deltas: list[float]) -> float:
    """Two-sided sign test on paired deltas (ties dropped)."""
    nonzero = [d for d in deltas if d != 0]
    n = len(nonzero)
    if n == 0:
        return 1.0
    k = sum(1 for d in nonzero if d < 0)
    tail = min(k, n - k)
    p = sum(comb(n, i) for i in range(0, tail + 1)) * 2 / (2 ** n)
    return min(p, 1.0)


def _final_text(arm_dir: Path, session_id: str) -> str | None:
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
    text = (final or {}).get("text")
    return text if isinstance(text, str) else None


def build_record(run_root: Path, case_id: str, dataset: Path) -> dict[str, object]:
    data = yaml.safe_load(dataset.read_text())
    case = next(c for c in data["cases"] if c["id"] == case_id)
    expected = (case.get("holdout") or {}).get("expected") or case.get("expected") or {}
    required = _as_required_groups(expected.get("required", []), "expected.required")
    forbidden = _as_string_list(expected.get("forbidden", []), "expected.forbidden")
    structured_raw = expected.get("structured", {})
    structured = structured_raw if isinstance(structured_raw, dict) else {}
    legacy_expected_raw = case.get("legacy_expected")
    legacy_expected = (
        legacy_expected_raw if isinstance(legacy_expected_raw, dict) else None
    )
    legacy_required = (
        _as_required_groups(
            legacy_expected.get("required", []),
            "legacy_expected.required",
        )
        if legacy_expected is not None
        else None
    )
    legacy_forbidden = (
        _as_string_list(
            legacy_expected.get("forbidden", []),
            "legacy_expected.forbidden",
        )
        if legacy_expected is not None
        else None
    )

    study = {}
    results = run_root / "results.json"
    if results.exists():
        study = json.loads(results.read_text())

    arms: list[dict] = []
    for path in sorted(run_root.rglob("private/result.json")):
        rec = json.loads(path.read_text())
        arm_dir = path.parent.parent
        text = _final_text(arm_dir, rec.get("holdout_session_id") or "")
        scored = score_arm_output(
            rec,
            text,
            required,
            structured,
            forbidden,
            legacy_required_terms=legacy_required,
            legacy_forbidden_terms=legacy_forbidden,
        )
        complete = bool(rec.get("holdout_complete"))
        excluded = None
        if rec.get("training_failure_reasons"):
            excluded = "training_failed"
        elif not complete:
            excluded = "incomplete_holdout"
        arms.append({
            "replica": rec.get("replica"),
            "condition": rec.get("condition"),
            "complete": complete,
            "excluded": excluded,
            "unmeasured": (
                "unrecoverable_answer_text" if complete and scored is None else None
            ),
            "raw_passed": rec.get("rubric_passed"),
            "corrected_passed": (bool(scored["passed"]) if scored else None),
            "corrected_missing": (scored or {}).get("missing"),
            "corrected_forbidden": (scored or {}).get("forbidden"),
            "scoring_mode": (
                scored.get("scoring_mode")
                if scored is not None
                else (
                    "structured"
                    if "structured_output" in rec
                    else "legacy_prose"
                )
            ),
            "rubric_provenance": (
                scored.get("rubric_provenance")
                if scored is not None
                else (
                    CURRENT_RUBRIC_PROVENANCE
                    if "structured_output" in rec
                    else LEGACY_RUBRIC_PROVENANCE
                )
            ),
            "cost_usd": rec.get("total_cost_usd"),
            "guidelines_retrieved": rec.get("guidelines_retrieved"),
        })

    def _rate(subset: list[dict], key: str) -> tuple[int, int, float | None]:
        vals = [a[key] for a in subset if a[key] is not None]
        passed = sum(1 for value in vals if value)
        rate = round(100.0 * passed / len(vals), 1) if vals else None
        return passed, len(vals), rate

    quality = {}
    for cond in ("memory", "control"):
        complete = [a for a in arms if a["condition"] == cond and a["complete"]]
        raw_pass_n, raw_scored_n, raw_pass_pct = _rate(complete, "raw_passed")
        corrected_pass_n, corrected_scored_n, corrected_pass_pct = _rate(
            complete,
            "corrected_passed",
        )
        quality[cond] = {
            "n_complete": len(complete),
            "raw_pass_n": raw_pass_n,
            "raw_scored_n": raw_scored_n,
            "raw_pass_pct": raw_pass_pct,
            "corrected_pass_n": corrected_pass_n,
            "corrected_scored_n": corrected_scored_n,
            "corrected_pass_pct": corrected_pass_pct,
            "corrected_basis_warning": (
                "single_scorable_arm" if corrected_scored_n == 1 else None
            ),
            "mean_cost_usd": (round(statistics.mean(
                [a["cost_usd"] for a in complete if isinstance(a["cost_usd"], (int, float))]), 6)
                if complete else None),
        }

    by_replica: dict[object, dict] = {}
    for a in arms:
        by_replica.setdefault(a["replica"], {})[a["condition"]] = a
    deltas, pairs = [], []
    for replica in sorted(by_replica, key=lambda r: (r is None, r)):
        mem, ctl = by_replica[replica].get("memory"), by_replica[replica].get("control")
        if not mem or not ctl:
            continue
        if mem["excluded"] or ctl["excluded"]:
            continue
        if not isinstance(mem["cost_usd"], (int, float)) or not isinstance(ctl["cost_usd"], (int, float)):
            continue
        delta = mem["cost_usd"] - ctl["cost_usd"]
        deltas.append(delta)
        pairs.append({"replica": replica, "delta_usd": round(delta, 6)})

    control_arms = [a for a in arms if a["condition"] == "control"]
    control_valid = bool(control_arms) and all(
        a["guidelines_retrieved"] == 0 for a in control_arms
        if a["guidelines_retrieved"] is not None
    )

    return {
        "company": study.get("company") or run_root.name,
        "case_id": case_id,
        "benchmark": "memory_vs_true_control",
        "generated_at": study.get("generated_at"),
        "fabri_version": study.get("fabri_version"),
        "roster_revision": study.get("roster_revision"),
        "replicas": study.get("replicas"),
        "arms_total": len(arms),
        "quality": quality,
        "cost": {
            "clean_pairs": len(deltas),
            "mean_delta_usd": round(statistics.mean(deltas), 6) if deltas else None,
            "median_delta_usd": round(statistics.median(deltas), 6) if deltas else None,
            "memory_cheaper_pairs": sum(1 for d in deltas if d < 0),
            "sign_test_p": round(_sign_test_p(deltas), 4) if deltas else None,
            "pairs": pairs,
        },
        "control_memory_free": control_valid,
        "scoring": [
            {
                "replica": arm["replica"],
                "condition": arm["condition"],
                "mode": arm["scoring_mode"],
                "rubric_provenance": arm["rubric_provenance"],
                "passed": arm["corrected_passed"],
                "missing": arm["corrected_missing"],
                "forbidden": arm["corrected_forbidden"],
            }
            for arm in arms
        ],
        "unmeasured_arms": [
            {
                "replica": arm["replica"],
                "condition": arm["condition"],
                "reason": arm["unmeasured"],
            }
            for arm in arms
            if arm["unmeasured"]
        ],
        "excluded_arms": [
            {"replica": a["replica"], "condition": a["condition"], "reason": a["excluded"]}
            for a in arms if a["excluded"]
        ],
        "total_spend_usd": round(sum(
            a["cost_usd"] for a in arms if isinstance(a["cost_usd"], (int, float))), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--case", dest="case_id", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    record = build_record(Path(args.run_root), args.case_id, Path(args.dataset))
    text = json.dumps(record, indent=2)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
