#!/usr/bin/env python3
"""Re-score completed study arms with the CURRENT rubric and report raw vs corrected.

The study records a rubric verdict at run time. When the scorer is later fixed (e.g. the
negation-aware forbidden window, or proximity matching for required terms), those recorded
verdicts become stale. This tool re-extracts each arm's actual holdout output from its trace
and re-scores it with the scorer as it exists NOW, so a run can be audited without re-spending.

Usage:
  python benchmarks/rescore_runs.py --run-root benchmarks/runs/<name> \
      --dataset benchmarks/datasets/company_memory_experiments.yaml [--case <case_id>]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from fabri.benchmarks.company_setup_probe import score_text


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


def _rubric_for_case(dataset: Path, case_id: str) -> tuple[list, list]:
    data = yaml.safe_load(dataset.read_text())
    case = next(c for c in data["cases"] if c["id"] == case_id)
    expected = (case.get("holdout") or {}).get("expected") or case.get("expected") or {}
    return expected.get("required") or [], expected.get("forbidden") or []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--case", dest="case_id", default=None)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    root = Path(args.run_root)
    case_id = args.case_id
    if case_id is None:
        results = next(root.rglob("results.json"), None)
        if results is not None:
            case_id = json.loads(results.read_text()).get("case_id")
    if case_id is None:
        raise SystemExit("could not infer --case; pass it explicitly")

    required, forbidden = _rubric_for_case(Path(args.dataset), case_id)

    rows = []
    for result_path in sorted(root.rglob("private/result.json")):
        arm_dir = result_path.parent.parent
        rec = json.loads(result_path.read_text())
        sid = rec.get("holdout_session_id")
        text = _final_text(arm_dir, sid) if sid else None
        raw_passed = rec.get("rubric_passed")
        if text is None:
            rows.append({
                "replica": rec.get("replica"), "condition": rec.get("condition"),
                "raw_passed": raw_passed, "corrected_passed": None,
                "note": "no FINAL text found", "cost": rec.get("total_cost_usd"),
            })
            continue
        scored = score_text(text, required, forbidden)
        rows.append({
            "replica": rec.get("replica"), "condition": rec.get("condition"),
            "raw_passed": raw_passed,
            "raw_missing": rec.get("missing_required"), "raw_forbidden": rec.get("forbidden_hits"),
            "corrected_passed": bool(scored["passed"]),
            "corrected_missing": scored["missing"], "corrected_forbidden": scored["forbidden"],
            "cost": rec.get("total_cost_usd"),
            "guidelines_retrieved": rec.get("guidelines_retrieved"),
        })

    def rate(rs, key):
        vals = [r[key] for r in rs if r.get(key) is not None]
        return (sum(1 for v in vals if v) / len(vals) * 100.0) if vals else float("nan")

    print(f"case: {case_id}   arms: {len(rows)}")
    print(f"{'replica':>7} {'condition':<9} {'raw':>5} {'corrected':>10}  flipped  missing(raw -> corrected)")
    for r in sorted(rows, key=lambda r: (r["condition"] or "", r["replica"] or 0)):
        flipped = "YES" if (r["raw_passed"] is not None and r["corrected_passed"] is not None
                            and r["raw_passed"] != r["corrected_passed"]) else ""
        print(f"{str(r['replica']):>7} {str(r['condition']):<9} {str(r['raw_passed']):>5} "
              f"{str(r['corrected_passed']):>10}  {flipped:<7}  "
              f"{r.get('raw_missing')} -> {r.get('corrected_missing')}")

    print("\n== pass rate by condition (raw -> corrected) ==")
    for cond in sorted({r["condition"] for r in rows if r["condition"]}):
        sub = [r for r in rows if r["condition"] == cond]
        costs = [r["cost"] for r in sub if isinstance(r.get("cost"), (int, float))]
        mean_cost = sum(costs) / len(costs) if costs else float("nan")
        print(f"  {cond:<9} n={len(sub):<3} raw={rate(sub,'raw_passed'):6.1f}%  "
              f"corrected={rate(sub,'corrected_passed'):6.1f}%   mean_cost=${mean_cost:.4f}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"case_id": case_id, "required": required, "forbidden": forbidden, "rows": rows},
            indent=2))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
