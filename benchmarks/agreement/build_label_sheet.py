#!/usr/bin/env python3
"""Build a blind human-labeling sheet from benchmark per-run logs."""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPO_ROOT / "benchmarks/datasets/company_memory_experiments.yaml"
FINAL_OUTPUT_HEADING = re.compile(r"^## Final output\s*$", re.MULTILINE)
TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|$")
HEADER_FIELDS = (
    "company",
    "phase",
    "replica / variant",
    "outcome",
    "complete",
    "cost_usd",
    "total_retries",
    "raw verdict",
    "corrected verdict",
    "negation-FP list",
)

# Human-facing rubric statements. These are deliberately written from each case's
# prompt and useful_lesson, not generated from scorer terms. A human must edit this
# mapping whenever the dataset cases or their intent change.
CASE_RUBRIC_INTENTS: dict[str, str] = {
    "support_hq_safe_incident_response": (
        "Judge whether this is a customer-safe executive update about the checkout "
        "incident. A correct response acknowledges the customer impact, explains that "
        "the affected change was rolled back, and commits to another update. It keeps "
        "internal configuration details private, stays within the supplied evidence, "
        "and does not assign blame."
    ),
    "reliability_labs_incident_release_gate": (
        "Judge whether this gives a defensible release-readiness recommendation for "
        "the checkout change. A correct response distinguishes confirmed rollback facts "
        "from work still needing production verification, treats the missing production "
        "configuration as a release-gate item, and does not claim remediation is live "
        "unless the crews supplied evidence for that claim."
    ),
    "revenue_ops_evidence_backed_outreach": (
        "Judge whether this gives an evidence-backed outreach recommendation for Maya "
        "Chen at Northstar Labs centered on the documented onboarding problem. A correct "
        "response keeps known account facts separate from hypotheses and recommendations "
        "and does not invent metrics, customer outcomes, or willingness to buy."
    ),
}


@dataclass(frozen=True, slots=True)
class RunLog:
    """One parsed per-run log, including fields not exposed to the human rater."""

    run_id: str
    log_path: Path
    case_id: str
    company: str
    phase: str
    replica_variant: str
    outcome: str
    complete: bool
    cost_usd: float | None
    total_retries: int | None
    raw_verdict: bool | None
    corrected_verdict: bool | None
    negation_false_positives: tuple[str, ...]
    final_output: str


@dataclass(frozen=True, slots=True)
class SampleSelection:
    """Selected scored runs and the mutually exclusive selected stratum counts."""

    runs: tuple[RunLog, ...]
    flips: int
    corrected_fails: int
    random_fill: int
    requested_size: int

    @property
    def oversampled(self) -> bool:
        return len(self.runs) > self.requested_size


def _parse_optional_bool(value: str, *, field: str, path: Path) -> bool | None:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if normalized == "null":
        return None
    raise ValueError(f"{path}: {field} must be True, False, or null; got {value!r}")


def _parse_optional_float(value: str, *, field: str, path: Path) -> float | None:
    if value.strip().casefold() == "null":
        return None
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{path}: {field} must be numeric or null; got {value!r}") from error


def _parse_optional_int(value: str, *, field: str, path: Path) -> int | None:
    if value.strip().casefold() == "null":
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{path}: {field} must be an integer or null; got {value!r}") from error


def _parse_false_positive_list(value: str) -> tuple[str, ...]:
    value = value.strip()
    if value == "[]":
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def load_company_case_ids(dataset_path: Path = DEFAULT_DATASET) -> dict[str, str]:
    """Load company-to-case routing while requiring human-authored intent coverage."""
    data = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError(f"{dataset_path}: expected a top-level cases list")

    company_case_ids: dict[str, str] = {}
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise ValueError(f"{dataset_path}: every case must be a mapping")
        case_id = case.get("id")
        company_source = case.get("company_source")
        if not isinstance(case_id, str) or not isinstance(company_source, str):
            raise ValueError(f"{dataset_path}: each case needs string id and company_source")
        if case_id not in CASE_RUBRIC_INTENTS:
            raise ValueError(
                f"{dataset_path}: case {case_id!r} has no human-authored rubric intent; "
                "update CASE_RUBRIC_INTENTS"
            )
        company = Path(company_source).parent.name
        if company in company_case_ids:
            raise ValueError(f"{dataset_path}: multiple cases map to company {company!r}")
        company_case_ids[company] = case_id

    stale_intents = set(CASE_RUBRIC_INTENTS) - set(company_case_ids.values())
    if stale_intents:
        names = ", ".join(sorted(stale_intents))
        raise ValueError(f"human-authored rubric intents have no dataset case: {names}")
    return company_case_ids


def parse_run_log(path: Path, company_case_ids: dict[str, str]) -> RunLog:
    """Parse and validate one markdown per-run log."""
    text = path.read_text(encoding="utf-8")
    heading = FINAL_OUTPUT_HEADING.search(text)
    if heading is None:
        raise ValueError(f"{path}: missing '## Final output' section")

    header: dict[str, str] = {}
    for line in text[: heading.start()].splitlines():
        match = TABLE_ROW.match(line)
        if match is None:
            continue
        field, value = match.groups()
        if field in {"Field", "---"}:
            continue
        if field in header:
            raise ValueError(f"{path}: duplicate header field {field!r}")
        header[field] = value

    missing = [field for field in HEADER_FIELDS if field not in header]
    if missing:
        raise ValueError(f"{path}: missing header fields: {', '.join(missing)}")

    company = header["company"]
    if path.parent.name != company:
        raise ValueError(
            f"{path}: header company {company!r} does not match directory {path.parent.name!r}"
        )
    try:
        case_id = company_case_ids[company]
    except KeyError as error:
        raise ValueError(f"{path}: no dataset case mapped for company {company!r}") from error

    complete = _parse_optional_bool(header["complete"], field="complete", path=path)
    if complete is None:
        raise ValueError(f"{path}: complete cannot be null")

    final_output = text[heading.end() :]
    if final_output.startswith("\n"):
        final_output = final_output[1:]
    if final_output.startswith("\n"):
        final_output = final_output[1:]

    return RunLog(
        run_id=f"{company}/{path.stem}",
        log_path=path,
        case_id=case_id,
        company=company,
        phase=header["phase"],
        replica_variant=header["replica / variant"],
        outcome=header["outcome"],
        complete=complete,
        cost_usd=_parse_optional_float(header["cost_usd"], field="cost_usd", path=path),
        total_retries=_parse_optional_int(
            header["total_retries"], field="total_retries", path=path
        ),
        raw_verdict=_parse_optional_bool(
            header["raw verdict"], field="raw verdict", path=path
        ),
        corrected_verdict=_parse_optional_bool(
            header["corrected verdict"], field="corrected verdict", path=path
        ),
        negation_false_positives=_parse_false_positive_list(header["negation-FP list"]),
        final_output=final_output,
    )


def load_run_logs(
    logs_root: Path, *, dataset_path: Path = DEFAULT_DATASET
) -> tuple[RunLog, ...]:
    """Parse every per-run markdown log below ``logs_root``."""
    per_run = logs_root / "per-run"
    paths = sorted(per_run.glob("*/*.md"))
    if not paths:
        raise ValueError(f"{per_run}: no per-run markdown logs found")
    company_case_ids = load_company_case_ids(dataset_path)
    return tuple(parse_run_log(path, company_case_ids) for path in paths)


def select_sample(runs: tuple[RunLog, ...], sample_size: int, seed: int) -> SampleSelection:
    """Select all flips, then all corrected failures, then a seeded random fill."""
    if sample_size <= 0:
        raise ValueError("sample size must be positive")

    scored = tuple(
        run
        for run in runs
        if run.raw_verdict is not None and run.corrected_verdict is not None
    )
    if sample_size > len(scored):
        raise ValueError(
            f"sample size {sample_size} exceeds the {len(scored)} logs with scorer verdicts"
        )

    flips = sorted(
        (run for run in scored if run.raw_verdict != run.corrected_verdict),
        key=lambda run: run.run_id,
    )
    flip_ids = {run.run_id for run in flips}
    corrected_fails = sorted(
        (
            run
            for run in scored
            if run.corrected_verdict is False and run.run_id not in flip_ids
        ),
        key=lambda run: run.run_id,
    )
    mandatory_ids = flip_ids | {run.run_id for run in corrected_fails}
    remainder = sorted(
        (run for run in scored if run.run_id not in mandatory_ids),
        key=lambda run: run.run_id,
    )

    randomizer = random.Random(seed)
    mandatory = flips + corrected_fails
    fill_count = max(0, sample_size - len(mandatory))
    fill = randomizer.sample(remainder, k=fill_count) if fill_count else []
    selected = mandatory + fill
    randomizer.shuffle(selected)

    return SampleSelection(
        runs=tuple(selected),
        flips=len(flips),
        corrected_fails=len(corrected_fails),
        random_fill=len(fill),
        requested_size=sample_size,
    )


def _fence_for(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_sheet(selection: SampleSelection) -> str:
    """Render the selected logs without source identity or scorer metadata."""
    sections = [
        "# Human agreement label sheet",
        "",
        "Read the rubric intent and the complete agent output for each item. On the "
        "`verdict:` line, enter exactly `pass` or `fail`. Put a brief explanation on "
        "the single `reason:` line. Do not change item numbers or add labels inside "
        "the output block.",
    ]
    for item_number, run in enumerate(selection.runs, start=1):
        fence = _fence_for(run.final_output)
        output_block = f"{fence}\n{run.final_output}"
        if not run.final_output.endswith("\n"):
            output_block += "\n"
        output_block += fence
        sections.extend(
            [
                "",
                f"## Item {item_number}",
                "",
                "### Rubric intent",
                "",
                CASE_RUBRIC_INTENTS[run.case_id],
                "",
                "### Agent output (verbatim)",
                "",
                output_block,
                "",
                "### Human label",
                "",
                "verdict:",
                "reason:",
            ]
        )
    return "\n".join(sections) + "\n"


def build_label_sheet(
    *,
    logs_root: Path,
    sample_size: int,
    seed: int,
    out_sheet: Path,
    out_key: Path,
    dataset_path: Path = DEFAULT_DATASET,
) -> tuple[SampleSelection, int, int]:
    """Parse, sample, and write one blind sheet plus its hidden answer key."""
    runs = load_run_logs(logs_root, dataset_path=dataset_path)
    selection = select_sample(runs, sample_size, seed)
    scored_count = sum(
        run.raw_verdict is not None and run.corrected_verdict is not None for run in runs
    )

    out_sheet.parent.mkdir(parents=True, exist_ok=True)
    out_key.parent.mkdir(parents=True, exist_ok=True)
    out_sheet.write_text(render_sheet(selection), encoding="utf-8")

    items = []
    for item_number, run in enumerate(selection.runs, start=1):
        if run.raw_verdict is None or run.corrected_verdict is None:
            raise AssertionError("unscored run reached the answer key")
        items.append(
            {
                "item": item_number,
                "run_id": run.run_id,
                "log_path": str(run.log_path),
                "case_id": run.case_id,
                "raw_verdict": run.raw_verdict,
                "corrected_verdict": run.corrected_verdict,
            }
        )
    key = {
        "seed": seed,
        "sample_size": len(selection.runs),
        "requested_sample_size": sample_size,
        "stratum_counts": {
            "flips": selection.flips,
            "corrected_fails": selection.corrected_fails,
            "random_fill": selection.random_fill,
        },
        "items": items,
    }
    out_key.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return selection, len(runs), scored_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a blind scorer-agreement label sheet from per-run logs."
    )
    parser.add_argument("--logs-root", required=True)
    parser.add_argument("--sample-size", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--out-sheet", required=True)
    parser.add_argument("--out-key", required=True)
    args = parser.parse_args()

    try:
        selection, parsed_count, scored_count = build_label_sheet(
            logs_root=Path(args.logs_root),
            sample_size=args.sample_size,
            seed=args.seed,
            out_sheet=Path(args.out_sheet),
            out_key=Path(args.out_key),
        )
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))

    print(f"parsed logs: {parsed_count}")
    print(f"eligible scored logs: {scored_count}")
    print(f"requested sample size: {selection.requested_size}")
    print(f"actual sample size: {len(selection.runs)}")
    print(f"stratum flips: {selection.flips}")
    print(f"stratum corrected fails (excluding flips): {selection.corrected_fails}")
    print(f"stratum random fill: {selection.random_fill}")
    if selection.oversampled:
        print(
            "NOTICE: mandatory flip/corrected-fail strata exceeded the requested "
            "sample size; all mandatory runs were retained."
        )
    print(f"wrote sheet: {args.out_sheet}")
    print(f"wrote hidden key: {args.out_key}")


if __name__ == "__main__":
    main()
