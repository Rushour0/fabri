"""Aggregate a completed company memory-vs-control live study into one report.

Reads the per-arm private ``result.json`` records written by
``company_memory_study.py`` directly from ``private-attempts/replica-NN/<condition>/``
so it works even when a company process died before writing its top-level
``results.json``. Produces a consolidated JSON payload plus a Markdown report.

Usage:
    python -m fabri.benchmarks.company_memory_report \
        --run-root benchmarks/runs/full-evolution-20260721 \
        --output benchmarks/runs/full-evolution-20260721/consolidated-report.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

try:  # optional; stdlib fallback covers the required sign test
    from scipy.stats import wilcoxon as _scipy_wilcoxon
except ImportError:  # pragma: no cover - exercised only when scipy is absent
    _scipy_wilcoxon = None

DEFAULT_RUN_ROOT = "benchmarks/runs/full-evolution-20260721"
_CONDITIONS = ("memory", "control")

COMPANIES: tuple[tuple[str, str, str], ...] = (
    ("support-hq", "support-hq-memory-control", "support_hq_safe_incident_response"),
    (
        "reliability-labs",
        "reliability-labs-memory-control",
        "reliability_labs_incident_release_gate",
    ),
    ("revenue-ops", "revenue-ops-memory-control", "revenue_ops_evidence_backed_outreach"),
)
ROOT_ID = "ceo"

_OUTCOME_RETRY_FIELDS = (
    "repair_retries",
    "structured_output_retries",
    "provider_transient_retries",
    "max_token_retries",
    "total_retries",
)


def reconcile_run(run: dict[str, object]) -> dict[str, object]:
    """Correct the known training_success/training_failure_reasons contradiction.

    Some live processes launched from an older module version could write a
    per-arm record with ``training_success: true`` while
    ``training_failure_reasons`` is non-empty (a truncated training run
    self-reported success). The failure reasons are authoritative: if both are
    present, force ``training_success`` to ``False``. Returns a shallow copy;
    the input is never mutated.
    """
    reconciled = dict(run)
    if reconciled.get("training_success") is True and reconciled.get(
        "training_failure_reasons"
    ):
        reconciled["training_success"] = False
    return reconciled


def _load_arm(path: Path) -> dict[str, object] | None:
    result_path = path / "private" / "result.json"
    if not result_path.is_file():
        return None
    with result_path.open() as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"malformed result.json at {result_path}")
    return reconcile_run(raw)


@dataclass(frozen=True)
class LoadedCompany:
    """The reconciled per-arm records for one company, keyed by condition."""

    company: str
    dir_name: str
    case_id: str
    attempted: dict[str, list[int]]
    finished: dict[str, dict[int, dict[str, object]]]


def load_company(run_root: Path, company: str, dir_name: str, case_id: str) -> LoadedCompany:
    """Load every per-arm result.json under one company's private-attempts dir."""
    attempts_dir = run_root / dir_name / "private-attempts"
    attempted: dict[str, list[int]] = {condition: [] for condition in _CONDITIONS}
    finished: dict[str, dict[int, dict[str, object]]] = {
        condition: {} for condition in _CONDITIONS
    }
    if not attempts_dir.is_dir():
        return LoadedCompany(company, dir_name, case_id, attempted, finished)

    for replica_dir in sorted(attempts_dir.glob("replica-*")):
        if not replica_dir.is_dir():
            continue
        try:
            replica = int(replica_dir.name.split("-")[-1])
        except ValueError:
            continue
        for condition in _CONDITIONS:
            condition_dir = replica_dir / condition
            if not condition_dir.is_dir():
                continue
            attempted[condition].append(replica)
            record = _load_arm(condition_dir)
            if record is not None:
                finished[condition][replica] = record
    return LoadedCompany(company, dir_name, case_id, attempted, finished)


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.fmean(values) if values else None


def _median(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def _completed_arms(finished: dict[int, dict[str, object]]) -> dict[int, dict[str, object]]:
    return {
        replica: record
        for replica, record in finished.items()
        if record.get("holdout_complete") is True
    }


def _specialist_reports(mining_reports: list[dict[str, object]]) -> list[dict[str, object]]:
    reports = []
    for report in mining_reports:
        producer = report.get("producer_agent_id")
        if producer is not None and producer != ROOT_ID:
            reports.append(report)
    return reports


def _condition_aggregate(
    condition: str,
    scheduled_replicas: int,
    attempted: list[int],
    finished: dict[int, dict[str, object]],
) -> dict[str, object]:
    completed = _completed_arms(finished)
    completed_records = list(completed.values())
    finished_records = list(finished.values())

    passes = [r for r in completed_records if r.get("rubric_passed") is True]
    rubric_pass_rate = (
        len(passes) / len(completed_records) if completed_records else None
    )

    completed_costs = [
        float(r["total_cost_usd"])
        for r in completed_records
        if r.get("total_cost_usd") is not None
    ]
    finished_costs = [
        float(r["total_cost_usd"])
        for r in finished_records
        if r.get("total_cost_usd") is not None
    ]

    retry_stats: dict[str, dict[str, float | None]] = {}
    for field in _OUTCOME_RETRY_FIELDS:
        values = []
        for record in completed_records:
            funnel = record.get("funnel")
            if not isinstance(funnel, dict):
                continue
            outcome = funnel.get("outcome")
            if not isinstance(outcome, dict):
                continue
            value = outcome.get(field)
            if isinstance(value, (int, float)):
                values.append(float(value))
        retry_stats[field] = {
            "mean": _mean(values),
            "sum": sum(values) if values else 0.0,
        }

    aggregate: dict[str, object] = {
        "scheduled_replicas": scheduled_replicas,
        "attempted_arms": len(attempted),
        "finished_arms": len(finished_records),
        "completed_arms": len(completed_records),
        "holdout_rubric_pass_rate": rubric_pass_rate,
        "median_total_cost_usd": _median(completed_costs),
        "mean_total_cost_usd": _mean(completed_costs),
        "mean_total_cost_usd_all_finished": _mean(finished_costs),
        "retries": retry_stats,
    }

    if condition == "memory":
        mining_reports_total = 0
        specialist_entry_ids: set[str] = set()
        transport_intact_flags: list[bool] = []
        per_arm_transported_rates: list[float] = []
        for record in finished.values():
            funnel = record.get("funnel")
            if not isinstance(funnel, dict):
                continue
            supply = funnel.get("supply")
            reports = []
            if isinstance(supply, dict):
                raw_reports = supply.get("mining_reports")
                if isinstance(raw_reports, list):
                    reports = _specialist_reports(raw_reports)
            mining_reports_total += len(reports)
            arm_specialist_ids: set[str] = set()
            for report in reports:
                for entry_id in report.get("entry_ids", []) or []:
                    if isinstance(entry_id, str):
                        arm_specialist_ids.add(entry_id)
            specialist_entry_ids |= arm_specialist_ids

            transport = funnel.get("transport")
            if isinstance(transport, dict) and isinstance(transport.get("intact"), bool):
                transport_intact_flags.append(bool(transport["intact"]))

            retrieval = funnel.get("retrieval")
            transported_retrieved: set[str] = set()
            if isinstance(retrieval, dict):
                raw_ids = retrieval.get("transported_entry_ids_retrieved")
                if isinstance(raw_ids, list):
                    transported_retrieved = {i for i in raw_ids if isinstance(i, str)}
            if arm_specialist_ids:
                hit = len(arm_specialist_ids & transported_retrieved)
                per_arm_transported_rates.append(hit / len(arm_specialist_ids))

        aggregate["memory_supply"] = {
            "specialist_mining_reports": mining_reports_total,
            "specialist_entry_ids_produced": len(specialist_entry_ids),
            "transport_intact_rate": _mean(
                [1.0 if flag else 0.0 for flag in transport_intact_flags]
            ),
            "transported_specialist_retrieval_rate_per_arm": per_arm_transported_rates,
            "mean_transported_specialist_retrieval_rate": _mean(per_arm_transported_rates),
            "verified_entries": None,
        }

    return aggregate


def _sign_test_p_value(n_pos: int, n_neg: int) -> float | None:
    """Two-sided exact sign-test p-value via the binomial distribution (ties dropped)."""
    n = n_pos + n_neg
    if n == 0:
        return None
    k = min(n_pos, n_neg)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def _paired_deltas(
    memory_completed: dict[int, dict[str, object]],
    control_completed: dict[int, dict[str, object]],
) -> dict[str, object]:
    shared_replicas = sorted(set(memory_completed) & set(control_completed))
    cost_deltas: list[float | None] = []
    rubric_deltas: list[int] = []
    guideline_deltas: list[float] = []

    for replica in shared_replicas:
        mem = memory_completed[replica]
        ctl = control_completed[replica]

        mem_cost = mem.get("total_cost_usd")
        ctl_cost = ctl.get("total_cost_usd")
        if isinstance(mem_cost, (int, float)) and isinstance(ctl_cost, (int, float)):
            cost_deltas.append(float(mem_cost) - float(ctl_cost))
        else:
            cost_deltas.append(None)

        mem_pass = 1 if mem.get("rubric_passed") is True else 0
        ctl_pass = 1 if ctl.get("rubric_passed") is True else 0
        rubric_deltas.append(mem_pass - ctl_pass)

        mem_guidelines = mem.get("guidelines_retrieved") or 0
        ctl_guidelines = ctl.get("guidelines_retrieved") or 0
        guideline_deltas.append(float(mem_guidelines) - float(ctl_guidelines))

    valid_cost_deltas = [d for d in cost_deltas if d is not None]

    def _sign_summary(deltas: list[float]) -> dict[str, object]:
        n_pos = sum(1 for d in deltas if d > 0)
        n_neg = sum(1 for d in deltas if d < 0)
        n_zero = sum(1 for d in deltas if d == 0)
        result: dict[str, object] = {
            "n_positive": n_pos,
            "n_negative": n_neg,
            "n_zero": n_zero,
            "sign_test_p_value": _sign_test_p_value(n_pos, n_neg),
        }
        if _scipy_wilcoxon is not None:
            non_zero = [d for d in deltas if d != 0]
            if len(non_zero) >= 1:
                try:
                    stat = _scipy_wilcoxon(non_zero)
                    result["wilcoxon_p_value"] = float(stat.pvalue)
                except ValueError:
                    pass
        return result

    return {
        "n_pairs": len(shared_replicas),
        "paired_replicas": shared_replicas,
        "cost_delta": {
            "values": cost_deltas,
            "mean": _mean(valid_cost_deltas),
            **_sign_summary(valid_cost_deltas),
        },
        "rubric_pass_delta": {
            "values": rubric_deltas,
            "mean": _mean([float(d) for d in rubric_deltas]),
            **_sign_summary([float(d) for d in rubric_deltas]),
        },
        "guidelines_retrieved_delta": {
            "values": guideline_deltas,
            "mean": _mean(guideline_deltas),
            **_sign_summary(guideline_deltas),
        },
    }


def build_company_report(loaded: LoadedCompany, scheduled_replicas: int = 10) -> dict[str, object]:
    condition_aggregates: dict[str, object] = {}
    for condition in _CONDITIONS:
        condition_aggregates[condition] = _condition_aggregate(
            condition,
            scheduled_replicas,
            loaded.attempted[condition],
            loaded.finished[condition],
        )

    memory_completed = _completed_arms(loaded.finished["memory"])
    control_completed = _completed_arms(loaded.finished["control"])
    paired = _paired_deltas(memory_completed, control_completed)

    return {
        "company": loaded.company,
        "dir_name": loaded.dir_name,
        "case_id": loaded.case_id,
        "conditions": condition_aggregates,
        "paired_deltas": paired,
    }


def build_report(run_root: Path, companies: Iterable[tuple[str, str, str]] = COMPANIES) -> dict[
    str, object
]:
    company_payloads: dict[str, object] = {}
    total_attempted = 0
    total_finished = 0
    total_completed = 0
    total_accounted_cost = 0.0
    notes = [
        "verified_entries is not derivable from the funnel as currently exposed; "
        "the funnel records mining/transport/retrieval events but no per-entry "
        "verification status. This must come from entry metadata (not fabricated here).",
        "Significance is assessed with an exact two-sided sign test over paired "
        "per-replica deltas (memory minus control), plus a Wilcoxon signed-rank "
        "p-value when scipy is available. Confidence-interval overlap across "
        "per-arm distributions is intentionally NOT used as a significance test.",
    ]

    for company, dir_name, case_id in companies:
        loaded = load_company(run_root, company, dir_name, case_id)
        company_report = build_company_report(loaded)
        company_payloads[company] = company_report

        for condition in _CONDITIONS:
            aggregate = company_report["conditions"][condition]
            total_attempted += aggregate["attempted_arms"]
            total_finished += aggregate["finished_arms"]
            total_completed += aggregate["completed_arms"]

        for condition in _CONDITIONS:
            for record in loaded.finished[condition].values():
                cost = record.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    total_accounted_cost += float(cost)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_root": str(run_root),
        "companies": company_payloads,
        "totals": {
            "total_attempted": total_attempted,
            "total_finished": total_finished,
            "total_completed": total_completed,
            "total_accounted_cost_usd": total_accounted_cost,
        },
        "notes": notes,
    }


def render_markdown(payload: Mapping[str, object]) -> str:
    """Render a readable report from an already-built payload."""

    def display(value: object, format_spec: str, prefix: str = "") -> str:
        return "—" if value is None else f"{prefix}{float(value):{format_spec}}"

    lines = [
        "# Company memory vs control — consolidated study report",
        "",
        f"- Run root: `{payload.get('run_root')}`",
        f"- Generated at: `{payload.get('generated_at')}`",
    ]

    totals = payload.get("totals")
    if isinstance(totals, dict):
        lines.append(
            f"- Totals: attempted={totals.get('total_attempted')}, "
            f"finished={totals.get('total_finished')}, "
            f"completed={totals.get('total_completed')}, "
            f"accounted cost=${totals.get('total_accounted_cost_usd', 0):.4f}"
        )
    lines.append("")

    companies = payload.get("companies")
    if isinstance(companies, dict):
        for company, report in companies.items():
            if not isinstance(report, dict):
                continue
            lines.append(f"## {company}")
            lines.append("")
            lines.append(f"- Case: `{report.get('case_id')}`")
            lines.append("")
            lines.append(
                "| Condition | Completed/Attempted | Rubric | Median cost | Mean cost | "
                "Mean total retries | Transport intact | Transported-specialist retrieval |"
            )
            lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            conditions = report.get("conditions")
            if isinstance(conditions, dict):
                for condition_name, aggregate in conditions.items():
                    if not isinstance(aggregate, dict):
                        continue
                    retries = aggregate.get("retries", {})
                    total_retry_mean = None
                    if isinstance(retries, dict):
                        total_retry = retries.get("total_retries")
                        if isinstance(total_retry, dict):
                            total_retry_mean = total_retry.get("mean")
                    supply = aggregate.get("memory_supply")
                    transport_rate = None
                    transported_rate = None
                    if isinstance(supply, dict):
                        transport_rate = supply.get("transport_intact_rate")
                        transported_rate = supply.get(
                            "mean_transported_specialist_retrieval_rate"
                        )
                    lines.append(
                        f"| {condition_name} | "
                        f"{aggregate.get('completed_arms')}/{aggregate.get('attempted_arms')} | "
                        f"{display(aggregate.get('holdout_rubric_pass_rate'), '.0%')} | "
                        f"{display(aggregate.get('median_total_cost_usd'), '.4f', '$')} | "
                        f"{display(aggregate.get('mean_total_cost_usd'), '.4f', '$')} | "
                        f"{display(total_retry_mean, '.2f')} | "
                        f"{display(transport_rate, '.0%')} | "
                        f"{display(transported_rate, '.0%')} |"
                    )
            lines.append("")

            paired = report.get("paired_deltas")
            if isinstance(paired, dict):
                cost_delta = paired.get("cost_delta", {})
                lines.append(
                    f"Paired deltas (memory − control), n_pairs={paired.get('n_pairs')}:"
                )
                if isinstance(cost_delta, dict):
                    memory_cheaper = cost_delta.get("n_negative")
                    lines.append(
                        f"- Mean cost delta: {display(cost_delta.get('mean'), '+.4f', '$')} "
                        f"(memory cheaper in {memory_cheaper} of {paired.get('n_pairs')} pairs); "
                        f"sign-test p={display(cost_delta.get('sign_test_p_value'), '.4f')}"
                    )
                rubric_delta = paired.get("rubric_pass_delta", {})
                if isinstance(rubric_delta, dict):
                    lines.append(
                        f"- Mean rubric-pass delta: {display(rubric_delta.get('mean'), '+.2f')}; "
                        f"sign-test p={display(rubric_delta.get('sign_test_p_value'), '.4f')}"
                    )
                lines.append("")

    lines.append("## Notes")
    lines.append("")
    notes = payload.get("notes")
    if isinstance(notes, list):
        for note in notes:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", default=DEFAULT_RUN_ROOT)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    run_root = Path(args.run_root)
    payload = build_report(run_root)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")

    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(render_markdown(payload))

    summary = {
        "run_root": str(run_root),
        "output": str(output_path),
        "markdown": str(markdown_path),
        "totals": payload["totals"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
