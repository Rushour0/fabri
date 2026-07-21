"""Offline, synthetic-fixture tests for company_memory_report.py."""
from __future__ import annotations

import json
from pathlib import Path

from fabri.benchmarks.company_memory_report import (
    build_company_report,
    load_company,
    reconcile_run,
    render_markdown,
)

COMPANY = "support-hq"
DIR_NAME = "support-hq-memory-control"
CASE_ID = "support_hq_safe_incident_response"


def _base_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "replica": 1,
        "condition": "memory",
        "holdout_complete": True,
        "rubric_passed": True,
        "missing_required": [],
        "forbidden_hits": [],
        "total_cost_usd": 0.05,
        "guidelines_retrieved": 4,
        "retrieval_candidate_kinds": ["postmortem"],
        "training_outcome": "success",
        "training_success": True,
        "training_failure_reasons": [],
        "holdout_failure_reasons": [],
        "execution_order": 1,
        "funnel": {
            "supply": {"mining_reports": [], "dbs": []},
            "transport": {"dbs": [], "intact": True},
            "retrieval": {
                "entry_ids": [],
                "transported_entry_ids_retrieved": [],
                "guidelines_retrieved": 4,
            },
            "outcome": {
                "rubric_passed": True,
                "repair_retries": 0,
                "structured_output_retries": 0,
                "provider_transient_retries": 0,
                "max_token_retries": 0,
                "total_retries": 0,
            },
        },
        "training_wall_time_s": 10.0,
        "holdout_wall_time_s": 5.0,
        "training_session_id": "train-1",
        "holdout_session_id": "holdout-1",
    }
    result.update(overrides)
    return result


def _write_arm(run_root: Path, replica: int, condition: str, record: dict[str, object]) -> None:
    private_dir = (
        run_root
        / DIR_NAME
        / "private-attempts"
        / f"replica-{replica:02d}"
        / condition
        / "private"
    )
    private_dir.mkdir(parents=True, exist_ok=True)
    (private_dir / "result.json").write_text(json.dumps(record))


def test_reconcile_run_fixes_contradictory_success_flag() -> None:
    contradictory = _base_result(
        holdout_complete=False,
        training_success=True,
        training_failure_reasons=["truncated_output"],
    )
    fixed = reconcile_run(contradictory)
    assert fixed["training_success"] is False
    # original is untouched (shallow copy contract)
    assert contradictory["training_success"] is True


def test_reconcile_run_leaves_consistent_records_alone() -> None:
    consistent = _base_result()
    fixed = reconcile_run(consistent)
    assert fixed["training_success"] is True
    assert fixed == consistent


def test_denominators_and_contradictory_arm_not_completed(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    # replica 1: memory completed+passed, control completed+passed -> a full pair
    _write_arm(run_root, 1, "memory", _base_result(replica=1, condition="memory"))
    _write_arm(
        run_root,
        1,
        "control",
        _base_result(
            replica=1,
            condition="control",
            total_cost_usd=0.08,
            guidelines_retrieved=0,
            funnel={
                "supply": {"mining_reports": [], "dbs": []},
                "transport": {"dbs": [], "intact": True},
                "retrieval": {
                    "entry_ids": [],
                    "transported_entry_ids_retrieved": [],
                    "guidelines_retrieved": 0,
                },
                "outcome": {
                    "rubric_passed": True,
                    "repair_retries": 1,
                    "structured_output_retries": 0,
                    "provider_transient_retries": 0,
                    "max_token_retries": 0,
                    "total_retries": 1,
                },
            },
        ),
    )

    # replica 2 memory: contradictory arm -> finished but must NOT count as completed
    _write_arm(
        run_root,
        2,
        "memory",
        _base_result(
            replica=2,
            condition="memory",
            holdout_complete=False,
            rubric_passed=None,
            training_success=True,
            training_failure_reasons=["truncated_output"],
        ),
    )
    # replica 2 control dir exists but attempt has no result.json yet (attempted, not finished)
    (
        run_root
        / DIR_NAME
        / "private-attempts"
        / "replica-02"
        / "control"
    ).mkdir(parents=True, exist_ok=True)

    loaded = load_company(run_root, COMPANY, DIR_NAME, CASE_ID)

    # attempted: 2 memory dirs, 2 control dirs
    assert len(loaded.attempted["memory"]) == 2
    assert len(loaded.attempted["control"]) == 2
    # finished: memory has 2 result.json (replica 1 + contradictory replica 2), control has 1
    assert len(loaded.finished["memory"]) == 2
    assert len(loaded.finished["control"]) == 1

    # the contradictory arm was reconciled at load time
    assert loaded.finished["memory"][2]["training_success"] is False

    report = build_company_report(loaded)
    memory_agg = report["conditions"]["memory"]
    control_agg = report["conditions"]["control"]

    assert memory_agg["attempted_arms"] == 2
    assert memory_agg["finished_arms"] == 2
    assert memory_agg["completed_arms"] == 1  # contradictory arm excluded
    assert control_agg["attempted_arms"] == 2
    assert control_agg["finished_arms"] == 1
    assert control_agg["completed_arms"] == 1

    # denominators are all distinct concepts here (2 != 2 != 1 for memory)
    assert memory_agg["attempted_arms"] == memory_agg["finished_arms"]
    assert memory_agg["finished_arms"] != memory_agg["completed_arms"]


def test_paired_deltas_and_no_ci_overlap_key(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    _write_arm(
        run_root,
        1,
        "memory",
        _base_result(replica=1, condition="memory", total_cost_usd=0.04, rubric_passed=True),
    )
    _write_arm(
        run_root,
        1,
        "control",
        _base_result(replica=1, condition="control", total_cost_usd=0.10, rubric_passed=False),
    )
    # replica 2: only memory completed -> not a pair
    _write_arm(
        run_root,
        2,
        "memory",
        _base_result(replica=2, condition="memory", total_cost_usd=0.03),
    )

    loaded = load_company(run_root, COMPANY, DIR_NAME, CASE_ID)
    report = build_company_report(loaded)
    paired = report["paired_deltas"]

    assert paired["n_pairs"] == 1
    assert paired["paired_replicas"] == [1]

    cost_delta = paired["cost_delta"]
    assert cost_delta["values"] == [0.04 - 0.10]
    assert cost_delta["mean"] == cost_delta["values"][0]
    assert "sign_test_p_value" in cost_delta
    assert "n_positive" in cost_delta and "n_negative" in cost_delta and "n_zero" in cost_delta

    rubric_delta = paired["rubric_pass_delta"]
    assert rubric_delta["values"] == [1]  # memory passed, control failed

    # explicit ban: no CI-overlap logic/keys anywhere in the payload
    def _walk_keys(obj: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(obj, dict):
            for key, value in obj.items():
                keys.add(str(key))
                keys |= _walk_keys(value)
        elif isinstance(obj, list):
            for item in obj:
                keys |= _walk_keys(item)
        return keys

    all_keys = _walk_keys(report)
    for forbidden in ("ci_overlap", "confidence_interval_overlap", "overlap"):
        assert forbidden not in all_keys


def test_specialist_supply_and_transported_retrieval_rate(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    mining_reports = [
        {
            "producer_agent_id": "ceo",  # manager supply, not specialist
            "qualifying_events": {"tool_failures": 0, "discrepancies": 0, "finals": 1},
            "candidates_produced": 1,
            "inserted": 1,
            "merged": 0,
            "skipped": 0,
            "skip_reasons": [],
            "entry_ids": ["manager-entry-1"],
        },
        {
            "producer_agent_id": "investigator",  # specialist
            "qualifying_events": {"tool_failures": 0, "discrepancies": 0, "finals": 1},
            "candidates_produced": 2,
            "inserted": 2,
            "merged": 0,
            "skipped": 0,
            "skip_reasons": [],
            "entry_ids": ["specialist-entry-1", "specialist-entry-2"],
        },
    ]
    record = _base_result(
        replica=1,
        condition="memory",
        funnel={
            "supply": {"mining_reports": mining_reports, "dbs": []},
            "transport": {"dbs": [], "intact": True},
            "retrieval": {
                "entry_ids": ["specialist-entry-1"],
                "transported_entry_ids_retrieved": ["specialist-entry-1"],
                "guidelines_retrieved": 1,
            },
            "outcome": {
                "rubric_passed": True,
                "repair_retries": 0,
                "structured_output_retries": 0,
                "provider_transient_retries": 0,
                "max_token_retries": 0,
                "total_retries": 0,
            },
        },
    )
    _write_arm(run_root, 1, "memory", record)

    loaded = load_company(run_root, COMPANY, DIR_NAME, CASE_ID)
    report = build_company_report(loaded)
    supply = report["conditions"]["memory"]["memory_supply"]

    assert supply["specialist_mining_reports"] == 1  # manager report excluded
    assert supply["specialist_entry_ids_produced"] == 2
    assert supply["transport_intact_rate"] == 1.0
    # 1 of 2 specialist entries was retrieved -> 0.5 for this arm
    assert supply["transported_specialist_retrieval_rate_per_arm"] == [0.5]
    assert supply["mean_transported_specialist_retrieval_rate"] == 0.5
    assert supply["verified_entries"] is None


def test_render_markdown_smoke(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _write_arm(run_root, 1, "memory", _base_result(replica=1, condition="memory"))
    _write_arm(run_root, 1, "control", _base_result(replica=1, condition="control"))

    loaded = load_company(run_root, COMPANY, DIR_NAME, CASE_ID)
    report = build_company_report(loaded)
    payload = {
        "generated_at": "2026-07-21T00:00:00+00:00",
        "run_root": str(run_root),
        "companies": {COMPANY: report},
        "totals": {
            "total_attempted": 2,
            "total_finished": 2,
            "total_completed": 2,
            "total_accounted_cost_usd": 0.1,
        },
        "notes": ["example note"],
    }
    markdown = render_markdown(payload)
    assert "support-hq" in markdown
    assert "Paired deltas" in markdown
    assert "example note" in markdown
