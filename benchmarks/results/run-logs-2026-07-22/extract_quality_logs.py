#!/usr/bin/env python3
"""Extract per-run final outputs and mechanically recompute frozen rubric verdicts."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import TypedDict


ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = ROOT / "benchmarks/runs/full-evolution-20260721"
OUTPUT_ROOT = Path(__file__).resolve().parent
RUBRIC_PATH = OUTPUT_ROOT / "rubric_terms.json"
NEGATION_RE = re.compile(
    r"\b(no|not|never|without|nor|neither|cannot|can not|n't|did not|does not|"
    r"do not|isn't|wasn't|aren't|weren't|absence of|lack of|unable|no evidence)\b",
    re.IGNORECASE,
)


class Payload(TypedDict, total=False):
    outcome: str
    final_text: str
    usage: dict[str, object]


class Result(TypedDict, total=False):
    holdout_complete: bool
    training_success: bool
    funnel: dict[str, object]


def normalize(value: str) -> str:
    """Apply the benchmark's frozen normalization procedure."""
    return re.sub(r"\s+", " ", value.casefold().replace("-", " ")).strip()


def read_json(path: Path, errors: list[str]) -> dict[str, object] | None:
    """Read one JSON payload without allowing a malformed file to stop extraction."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: {type(error).__name__}: {error}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{path}: expected a JSON object, got {type(data).__name__}")
        return None
    return data


def as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def optional_number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def payload_metrics(payload: dict[str, object]) -> tuple[str | None, int | float | None, str]:
    usage = payload.get("usage")
    cost = optional_number(usage.get("cost_usd")) if isinstance(usage, dict) else None
    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), str) else None
    final_text = as_text(payload.get("final_text"))
    return outcome, cost, final_text


def retry_count(result: dict[str, object] | None) -> int | float | None:
    if not isinstance(result, dict):
        return None
    funnel = result.get("funnel")
    outcome = funnel.get("outcome") if isinstance(funnel, dict) else None
    return optional_number(outcome.get("total_retries")) if isinstance(outcome, dict) else None


def score(
    final_text: str, required: list[list[str]], forbidden: list[str]
) -> tuple[list[str], list[str], list[str], bool, bool, list[str]]:
    """Score a holdout exactly, including the specified negation correction."""
    normalized_text = normalize(final_text)
    missing = [
        group[0]
        for group in required
        if not any(normalize(term) in normalized_text for term in group)
    ]
    raw_forbidden = [term for term in forbidden if normalize(term) in normalized_text]
    false_positives: list[str] = []
    for phrase in raw_forbidden:
        normalized_phrase = normalize(phrase)
        positions: list[int] = []
        start = 0
        while True:
            position = normalized_text.find(normalized_phrase, start)
            if position < 0:
                break
            positions.append(position)
            start = position + 1
        if positions and all(NEGATION_RE.search(normalized_text[max(0, position - 60):position]) for position in positions):
            false_positives.append(phrase)
    corrected_forbidden = [term for term in raw_forbidden if term not in false_positives]
    raw_pass = not missing and not raw_forbidden
    corrected_pass = not missing and not corrected_forbidden
    return missing, raw_forbidden, corrected_forbidden, raw_pass, corrected_pass, false_positives


def rubric_terms(rubric: dict[str, object]) -> tuple[list[list[str]], list[str]]:
    """Validate the frozen rubric shape at the boundary before scoring it."""
    required_value = rubric.get("required")
    forbidden_value = rubric.get("forbidden")
    if not isinstance(required_value, list) or not isinstance(forbidden_value, list):
        raise ValueError("rubric required/forbidden terms are malformed")
    required: list[list[str]] = []
    for group in required_value:
        if not isinstance(group, list) or not group or not all(isinstance(term, str) for term in group):
            raise ValueError("rubric required group is malformed")
        required.append(group)
    if not all(isinstance(term, str) for term in forbidden_value):
        raise ValueError("rubric forbidden terms are malformed")
    return required, forbidden_value


def markdown_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "[]"
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_log(record: dict[str, object], final_text: str) -> None:
    log_dir = OUTPUT_ROOT / "per-run" / str(record["company"])
    log_dir.mkdir(parents=True, exist_ok=True)
    name = f"{record['phase']}__{record['replica']}"
    if record["variant"] is not None:
        name += f"__{record['variant']}"
    header = [
        ("company", record["company"]),
        ("phase", record["phase"]),
        ("replica / variant", f"{record['replica']} / {record['variant'] or 'null'}"),
        ("outcome", record["outcome"]),
        ("complete", record["complete"]),
        ("cost_usd", record["cost_usd"]),
        ("total_retries", record["total_retries"]),
        ("raw verdict", record["raw_pass"]),
        ("corrected verdict", record["corrected_pass"]),
        ("negation-FP list", record["negation_false_positives"]),
    ]
    table = "\n".join(f"| {key} | {markdown_value(value)} |" for key, value in header)
    (log_dir / f"{name}.md").write_text(
        f"| Field | Value |\n| --- | --- |\n{table}\n\n## Final output\n\n{final_text}",
        encoding="utf-8",
    )


def make_record(
    *,
    company: str,
    phase: str,
    replica: str,
    variant: str | None,
    complete: bool,
    outcome: str | None,
    cost_usd: int | float | None,
    total_retries: int | float | None,
    final_text: str,
    rubric: dict[str, object] | None,
) -> dict[str, object]:
    if rubric is None:
        missing: list[str] | None = None
        forbidden: list[str] | None = None
        corrected: list[str] | None = None
        raw_pass: bool | None = None
        corrected_pass: bool | None = None
        false_positives: list[str] = []
    else:
        required, forbidden_terms = rubric_terms(rubric)
        missing, forbidden, corrected, raw_pass, corrected_pass, false_positives = score(
            final_text,
            required,
            forbidden_terms,
        )
    return {
        "company": company,
        "phase": phase,
        "replica": replica,
        "variant": variant,
        "complete": complete,
        "outcome": outcome,
        "cost_usd": cost_usd,
        "total_retries": total_retries,
        "char_len": len(final_text),
        "raw_missing": missing,
        "raw_forbidden": forbidden,
        "corrected_forbidden": corrected,
        "raw_pass": raw_pass,
        "corrected_pass": corrected_pass,
        "negation_false_positives": false_positives,
    }


def company_rubrics(rubrics: dict[str, object]) -> dict[str, dict[str, object]]:
    mapped: dict[str, dict[str, object]] = {}
    for case in rubrics.values():
        if isinstance(case, dict) and isinstance(case.get("company"), str):
            mapped[case["company"]] = case
    return mapped


def extract_study(company: str, case: dict[str, object], records: list[dict[str, object]], errors: list[str]) -> None:
    attempts = RUNS_ROOT / f"{company}-memory-control/private-attempts"
    for replica_dir in sorted(attempts.glob("replica-*")):
        for condition in ("memory", "control"):
            private_dir = replica_dir / condition / "private"
            result_path = private_dir / "result.json"
            result = read_json(result_path, errors) if result_path.exists() else None
            for kind, stdout_name, phase in (
                ("holdout", "holdout-run.stdout", f"study-{condition}-holdout"),
                ("training", "training-run.stdout", f"study-{condition}-training"),
            ):
                stdout_path = private_dir / stdout_name
                if not stdout_path.exists():
                    continue
                payload = read_json(stdout_path, errors)
                if payload is None:
                    continue
                outcome, cost_usd, final_text = payload_metrics(payload)
                if kind == "holdout":
                    complete = bool(result.get("holdout_complete")) if result is not None else outcome == "success"
                    rubric = {
                        "required": case["holdout_required"],
                        "forbidden": case["holdout_forbidden"],
                    }
                else:
                    complete = bool(result.get("training_success")) if result is not None else outcome == "success"
                    rubric = None
                record = make_record(
                    company=company,
                    phase=phase,
                    replica=replica_dir.name,
                    variant=None,
                    complete=complete,
                    outcome=outcome,
                    cost_usd=cost_usd,
                    total_retries=retry_count(result),
                    final_text=final_text,
                    rubric=rubric,
                )
                records.append(record)
                write_log(record, final_text)


def extract_evolution(company: str, case: dict[str, object], records: list[dict[str, object]], errors: list[str]) -> None:
    attempts = RUNS_ROOT / f"{company}-evolution/evolution/private-attempts"
    variants = case.get("variants")
    if not isinstance(variants, dict):
        errors.append(f"rubric for {company}: missing variants object")
        return
    for variant_dir in sorted(path for path in attempts.iterdir() if path.is_dir()) if attempts.exists() else []:
        variant_rubric = variants.get(variant_dir.name)
        if not isinstance(variant_rubric, dict):
            errors.append(f"{variant_dir}: no frozen rubric variant")
            continue
        for replica_dir in sorted(variant_dir.glob("replica-*")):
            for filename, phase in (("incumbent.stdout", "evo-incumbent"), ("candidate.stdout", "evo-candidate")):
                stdout_path = replica_dir / "private" / filename
                if not stdout_path.exists():
                    continue
                payload = read_json(stdout_path, errors)
                if payload is None:
                    continue
                outcome, cost_usd, final_text = payload_metrics(payload)
                record = make_record(
                    company=company,
                    phase=phase,
                    replica=replica_dir.name,
                    variant=variant_dir.name,
                    complete=outcome == "success",
                    outcome=outcome,
                    cost_usd=cost_usd,
                    total_retries=None,
                    final_text=final_text,
                    rubric=variant_rubric,
                )
                records.append(record)
                write_log(record, final_text)


def build_summary(records: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        grouped[(str(record["company"]), str(record["phase"]))].append(record)
    summary: list[dict[str, object]] = []
    for (company, phase), group in sorted(grouped.items()):
        costs = [float(cost) for record in group if (cost := record["cost_usd"]) is not None]
        char_lengths = [int(record["char_len"]) for record in group]
        summary.append(
            {
                "company": company,
                "phase": phase,
                "n": len(group),
                "raw_pass_count": sum(record["raw_pass"] is True for record in group),
                "corrected_pass_count": sum(record["corrected_pass"] is True for record in group),
                "mean_cost": fmean(costs) if costs else None,
                "mean_char_len": fmean(char_lengths) if char_lengths else None,
            }
        )
    return summary


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    raw_rubrics = read_json(RUBRIC_PATH, errors)
    if raw_rubrics is None:
        print("Could not load frozen rubric; no output written.", file=sys.stderr)
        return 1
    records: list[dict[str, object]] = []
    for company, case in sorted(company_rubrics(raw_rubrics).items()):
        extract_study(company, case, records, errors)
        extract_evolution(company, case, records, errors)
    records.sort(key=lambda record: (str(record["company"]), str(record["phase"]), str(record["variant"]), str(record["replica"])))
    summary = build_summary(records)
    (OUTPUT_ROOT / "index.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    phase_counts: dict[str, int] = defaultdict(int)
    for record in records:
        phase_counts[str(record["phase"])] += 1
    print(json.dumps({"runs_written_by_phase": dict(sorted(phase_counts.items())), "parse_errors": errors}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
