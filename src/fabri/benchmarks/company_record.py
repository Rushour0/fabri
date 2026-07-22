"""Normalize a completed company study into ONE tracked benchmark record.

A study run is a directory of per-arm results. For tracking a company over time we
want a single, stable, machine-readable row per (company, date, benchmark) that can be
appended to a history file and rendered on the catalog site.

Key properties this record captures that a raw pass-rate hides:
  * raw vs corrected quality (the recorded verdict vs a re-score with the CURRENT rubric)
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
from math import comb
from pathlib import Path

import yaml

from fabri.benchmarks.company_setup_probe import score_text


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


def build_record(run_root: Path, case_id: str, dataset: Path) -> dict:
    data = yaml.safe_load(dataset.read_text())
    case = next(c for c in data["cases"] if c["id"] == case_id)
    expected = (case.get("holdout") or {}).get("expected") or case.get("expected") or {}
    required = expected.get("required") or []
    forbidden = expected.get("forbidden") or []

    study = {}
    results = run_root / "results.json"
    if results.exists():
        study = json.loads(results.read_text())

    arms: list[dict] = []
    for path in sorted(run_root.rglob("private/result.json")):
        rec = json.loads(path.read_text())
        arm_dir = path.parent.parent
        text = _final_text(arm_dir, rec.get("holdout_session_id") or "")
        scored = score_text(text, required, forbidden) if text else None
        excluded = None
        if rec.get("training_failure_reasons"):
            excluded = "training_failed"
        elif not rec.get("holdout_complete"):
            excluded = "incomplete_holdout"
        arms.append({
            "replica": rec.get("replica"),
            "condition": rec.get("condition"),
            "complete": bool(rec.get("holdout_complete")),
            "excluded": excluded,
            "raw_passed": rec.get("rubric_passed"),
            "corrected_passed": (bool(scored["passed"]) if scored else None),
            "corrected_missing": (scored or {}).get("missing"),
            "corrected_forbidden": (scored or {}).get("forbidden"),
            "cost_usd": rec.get("total_cost_usd"),
            "guidelines_retrieved": rec.get("guidelines_retrieved"),
        })

    def _rate(subset: list[dict], key: str) -> float | None:
        vals = [a[key] for a in subset if a[key] is not None]
        return round(100.0 * sum(1 for v in vals if v) / len(vals), 1) if vals else None

    quality = {}
    for cond in ("memory", "control"):
        complete = [a for a in arms if a["condition"] == cond and a["complete"]]
        quality[cond] = {
            "n_complete": len(complete),
            "raw_pass_pct": _rate(complete, "raw_passed"),
            "corrected_pass_pct": _rate(complete, "corrected_passed"),
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
