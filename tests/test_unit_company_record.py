from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

# LiteLLM otherwise fetches a remote pricing map while importing the scorer.
_previous_local_cost_map = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
try:
    from fabri.benchmarks.company_record import _final_text, _sign_test_p, build_record
finally:
    if _previous_local_cost_map is None:
        os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    else:
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = _previous_local_cost_map

from tests.fixtures.benchmark_runs import (
    CASE_ID,
    PASSING_TEXT,
    write_arm,
    write_dataset as _fixture_write_dataset,
    write_run,
)

pytestmark = pytest.mark.unit


def write_dataset(
    directory: Path,
    *,
    case_id: str = CASE_ID,
    required: Sequence[object] = (
        ("checkout",),
        ("rollback", "rolled back"),
        ("follow-up",),
    ),
    forbidden: Sequence[str] = ("blame",),
) -> Path:
    """Write current and archived rubrics for tests that score legacy prose."""
    path = _fixture_write_dataset(
        directory,
        case_id=case_id,
        required=required,
        forbidden=forbidden,
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["cases"][0]["legacy_expected"] = {
        "required": list(required),
        "forbidden": list(forbidden),
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _set_structured_rubric(dataset: Path) -> None:
    data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    data["cases"][0]["expected"]["structured"] = {"decision": "READY", "tier": "SAFE"}
    dataset.write_text(yaml.safe_dump(data), encoding="utf-8")


def _set_structured_output(arm_dir: Path, value: object) -> None:
    result_path = arm_dir / "private" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["structured_output"] = value
    result_path.write_text(json.dumps(result), encoding="utf-8")


def test_sign_test_is_one_when_evenly_split() -> None:
    # 2 cheaper / 2 pricier -> no evidence of a direction.
    assert _sign_test_p([-1.0, -1.0, 1.0, 1.0]) == 1.0


def test_sign_test_drops_ties() -> None:
    # Zero deltas carry no sign information and must not inflate n.
    assert _sign_test_p([0.0, 0.0, -1.0, -1.0]) == _sign_test_p([-1.0, -1.0])


def test_sign_test_all_one_direction_is_small_but_not_significant_at_n4() -> None:
    # 0/4 in one direction is p=0.125 two-sided -- suggestive, NOT significant.
    assert _sign_test_p([1.0, 1.0, 1.0, 1.0]) == pytest.approx(0.125)


def test_sign_test_needs_enough_pairs_to_ever_be_significant() -> None:
    # With 5 pairs all one way the two-sided p is 0.0625 -- still above 0.05.
    # This guards against ever reporting "significant" off a tiny run.
    assert _sign_test_p([1.0] * 5) == pytest.approx(0.0625)
    assert _sign_test_p([1.0] * 6) < 0.05


def test_sign_test_empty_is_one() -> None:
    assert _sign_test_p([]) == 1.0


def test_training_failure_excludes_arm_and_replica_from_paired_cost(tmp_path: Path) -> None:
    """A training failure must never move a published paired-cost p-value."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(
        run_root,
        replica=1,
        condition="memory",
        training_failure_reasons=("training timeout",),
        total_cost_usd=1.0,
    )
    write_arm(run_root, replica=1, condition="control", total_cost_usd=3.0)
    write_arm(run_root, replica=2, condition="memory", total_cost_usd=2.0)
    write_arm(run_root, replica=2, condition="control", total_cost_usd=1.0)

    record = build_record(run_root, CASE_ID, dataset)

    assert record["excluded_arms"] == [
        {"replica": 1, "condition": "memory", "reason": "training_failed"}
    ]
    assert record["cost"]["clean_pairs"] == 1
    assert record["cost"]["pairs"] == [{"replica": 2, "delta_usd": 1.0}]
    assert record["cost"]["sign_test_p"] == 1.0


def test_incomplete_holdout_is_excluded_from_cost_and_complete_quality(tmp_path: Path) -> None:
    """An incomplete holdout must be labeled and omitted from complete-arm quality."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(
        run_root,
        replica=1,
        condition="memory",
        holdout_complete=False,
        total_cost_usd=1.0,
    )
    write_arm(run_root, replica=1, condition="control", total_cost_usd=2.0)

    record = build_record(run_root, CASE_ID, dataset)

    assert record["excluded_arms"] == [
        {"replica": 1, "condition": "memory", "reason": "incomplete_holdout"}
    ]
    assert record["quality"]["memory"]["n_complete"] == 0
    assert record["cost"]["clean_pairs"] == 0
    assert record["cost"]["pairs"] == []


def test_unmatched_replica_does_not_create_a_cost_pair(tmp_path: Path) -> None:
    """A replica observed in only one condition must not become an unpaired comparison."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=7, condition="memory", total_cost_usd=0.5)

    record = build_record(run_root, CASE_ID, dataset)

    assert record["cost"]["clean_pairs"] == 0
    assert record["cost"]["pairs"] == []
    assert record["cost"]["mean_delta_usd"] is None
    assert record["cost"]["sign_test_p"] is None


def test_missing_or_non_numeric_cost_does_not_create_a_pair(tmp_path: Path) -> None:
    """A pair needs numeric costs in both arms and malformed costs must not crash scoring."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="memory", total_cost_usd=None)
    write_arm(run_root, replica=1, condition="control", total_cost_usd=1.0)
    write_arm(run_root, replica=2, condition="memory", total_cost_usd=2.0)
    write_arm(run_root, replica=2, condition="control", total_cost_usd="unknown")

    record = build_record(run_root, CASE_ID, dataset)

    assert record["cost"]["clean_pairs"] == 0
    assert record["cost"]["pairs"] == []
    assert record["cost"]["sign_test_p"] is None
    assert record["total_spend_usd"] == 3.0


def test_control_memory_free_reflects_positive_zero_and_unknown_counts(tmp_path: Path) -> None:
    """Control retrieval must reflect counts; all-None currently passes, which is unsafe."""
    dataset = write_dataset(tmp_path)

    contaminated = write_run(tmp_path, name="contaminated")
    write_arm(contaminated, replica=1, condition="control", guidelines_retrieved=0)
    write_arm(contaminated, replica=2, condition="control", guidelines_retrieved=1)

    clean = write_run(tmp_path, name="clean")
    write_arm(clean, replica=1, condition="control", guidelines_retrieved=0)
    write_arm(clean, replica=2, condition="control", guidelines_retrieved=0)

    unknown = write_run(tmp_path, name="unknown")
    write_arm(unknown, replica=1, condition="control", guidelines_retrieved=None)
    write_arm(unknown, replica=2, condition="control", guidelines_retrieved=None)

    assert build_record(contaminated, CASE_ID, dataset)["control_memory_free"] is False
    assert build_record(clean, CASE_ID, dataset)["control_memory_free"] is True
    # This is the actual vacuous-all behavior, not the behavior we recommend.
    assert build_record(unknown, CASE_ID, dataset)["control_memory_free"] is True


def test_missing_trace_or_final_keeps_corrected_rate_unknown(tmp_path: Path) -> None:
    """Absent FINAL evidence must publish unknown corrected quality, never a false failure."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    missing_trace = write_arm(
        run_root,
        replica=1,
        condition="memory",
        trace_events=None,
    )
    missing_final = write_arm(
        run_root,
        replica=2,
        condition="memory",
        trace_events=({"type": "USAGE", "cost_usd": 1.0},),
    )

    record = build_record(run_root, CASE_ID, dataset)

    assert _final_text(missing_trace, "memory-1-session") is None
    assert _final_text(missing_final, "memory-2-session") is None
    assert record["quality"]["memory"] == {
        "n_complete": 2,
        "raw_pass_n": 2,
        "raw_scored_n": 2,
        "raw_pass_pct": 100.0,
        "corrected_pass_n": 0,
        "corrected_scored_n": 0,
        "corrected_pass_pct": None,
        "corrected_basis_warning": None,
        "mean_cost_usd": 1.0,
    }
    assert record["unmeasured_arms"] == [
        {
            "replica": 1,
            "condition": "memory",
            "reason": "unrecoverable_answer_text",
        },
        {
            "replica": 2,
            "condition": "memory",
            "reason": "unrecoverable_answer_text",
        },
    ]
    serialized = json.dumps(record, allow_nan=False)
    assert "NaN" not in serialized


def test_quality_rate_reports_two_scorable_arms_out_of_five_complete(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="memory")
    write_arm(
        run_root,
        replica=2,
        condition="memory",
        trace_events=({"type": "FINAL", "text": "blame"},),
    )
    for replica in range(3, 6):
        write_arm(
            run_root,
            replica=replica,
            condition="memory",
            trace_events=None,
        )

    record = build_record(run_root, CASE_ID, dataset)

    assert record["quality"]["memory"] == {
        "n_complete": 5,
        "raw_pass_n": 5,
        "raw_scored_n": 5,
        "raw_pass_pct": 100.0,
        "corrected_pass_n": 1,
        "corrected_scored_n": 2,
        "corrected_pass_pct": 50.0,
        "corrected_basis_warning": None,
        "mean_cost_usd": 1.0,
    }
    assert record["unmeasured_arms"] == [
        {
            "replica": replica,
            "condition": "memory",
            "reason": "unrecoverable_answer_text",
        }
        for replica in range(3, 6)
    ]


def test_final_text_falls_back_to_noncanonical_trace_path(tmp_path: Path) -> None:
    """Relocated archived traces must remain discoverable through the recursive fallback."""
    run_root = write_run(tmp_path)
    arm_dir = write_arm(
        run_root,
        replica=1,
        condition="memory",
        trace_subdir=Path("archive/relocated-traces"),
    )

    assert _final_text(arm_dir, "memory-1-session") == PASSING_TEXT


def test_final_text_uses_last_final_event(tmp_path: Path) -> None:
    """When retries emit several FINAL events, the last event is authoritative."""
    run_root = write_run(tmp_path)
    arm_dir = write_arm(
        run_root,
        replica=1,
        condition="memory",
        trace_events=(
            {"type": "FINAL", "text": "stale draft"},
            {"type": "USAGE", "cost_usd": 1.0},
            {"type": "final", "text": "authoritative final"},
        ),
    )

    assert _final_text(arm_dir, "memory-1-session") == "authoritative final"


def test_final_text_skips_malformed_json_lines(tmp_path: Path) -> None:
    """One corrupt trace line must not hide a later valid FINAL response."""
    run_root = write_run(tmp_path)
    arm_dir = write_arm(
        run_root,
        replica=1,
        condition="memory",
        trace_events=("{definitely not json", {"type": "FINAL", "text": "usable output"}),
    )

    assert _final_text(arm_dir, "memory-1-session") == "usable output"


def test_quality_rates_use_only_complete_arms_and_none_for_empty_condition(
    tmp_path: Path,
) -> None:
    """Quality denominators include only complete arms and empty denominators stay unknown."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(
        run_root,
        replica=1,
        condition="memory",
        rubric_passed=False,
        trace_events=({"type": "FINAL", "text": PASSING_TEXT},),
    )
    write_arm(
        run_root,
        replica=2,
        condition="memory",
        holdout_complete=False,
        rubric_passed=True,
        trace_events=({"type": "FINAL", "text": "blame"},),
    )
    write_arm(
        run_root,
        replica=1,
        condition="control",
        holdout_complete=False,
        rubric_passed=True,
    )

    record = build_record(run_root, CASE_ID, dataset)

    assert record["quality"]["memory"] == {
        "n_complete": 1,
        "raw_pass_n": 0,
        "raw_scored_n": 1,
        "raw_pass_pct": 0.0,
        "corrected_pass_n": 1,
        "corrected_scored_n": 1,
        "corrected_pass_pct": 100.0,
        "corrected_basis_warning": "single_scorable_arm",
        "mean_cost_usd": 1.0,
    }
    assert record["quality"]["control"] == {
        "n_complete": 0,
        "raw_pass_n": 0,
        "raw_scored_n": 0,
        "raw_pass_pct": None,
        "corrected_pass_n": 0,
        "corrected_scored_n": 0,
        "corrected_pass_pct": None,
        "corrected_basis_warning": None,
        "mean_cost_usd": None,
    }


@pytest.mark.parametrize(
    ("structured_output", "passed", "missing", "forbidden"),
    [
        (
            {"response": "Customer-safe note.", "decision": "READY", "tier": "SAFE"},
            True,
            [],
            [],
        ),
        (
            {"response": "Customer-safe note.", "decision": "READY"},
            False,
            ["missing:tier"],
            [],
        ),
        (None, False, ["structured_output:not_a_mapping"], []),
        (
            {"response": "We assign blame.", "decision": "READY", "tier": "SAFE"},
            False,
            [],
            ["blame"],
        ),
    ],
)
def test_build_record_scores_present_structure_and_response_prose(
    tmp_path: Path,
    structured_output: object,
    passed: bool,
    missing: list[str],
    forbidden: list[str],
) -> None:
    dataset = write_dataset(tmp_path)
    _set_structured_rubric(dataset)
    run_root = write_run(tmp_path)
    arm_dir = write_arm(run_root, replica=1, condition="memory")
    _set_structured_output(arm_dir, structured_output)

    record = build_record(run_root, CASE_ID, dataset)

    assert record["quality"]["memory"]["corrected_pass_pct"] == (
        100.0 if passed else 0.0
    )
    assert record["scoring"] == [
        {
            "replica": 1,
            "condition": "memory",
            "mode": "structured",
            "rubric_provenance": "expected",
            "passed": passed,
            "missing": missing,
            "forbidden": forbidden,
        }
    ]


def test_build_record_absent_structure_uses_legacy_prose_with_provenance(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(tmp_path)
    _set_structured_rubric(dataset)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="control")

    record = build_record(run_root, CASE_ID, dataset)

    assert record["quality"]["control"]["corrected_pass_pct"] == 100.0
    assert record["scoring"] == [
        {
            "replica": 1,
            "condition": "control",
            "mode": "legacy_prose",
            "rubric_provenance": "legacy_expected",
            "passed": True,
            "missing": [],
            "forbidden": [],
        }
    ]


def test_build_record_legacy_prose_requires_an_explicit_archive_rubric(
    tmp_path: Path,
) -> None:
    dataset = _fixture_write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="control")

    with pytest.raises(ValueError, match="legacy_expected is required"):
        build_record(run_root, CASE_ID, dataset)


def test_build_record_selects_rubric_by_scoring_mode(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path, forbidden=("current-only",))
    data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    data["cases"][0]["legacy_expected"]["forbidden"] = ["legacy-only"]
    dataset.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    run_root = write_run(tmp_path)
    write_arm(
        run_root,
        replica=1,
        condition="control",
        trace_events=({"type": "FINAL", "text": f"{PASSING_TEXT} current-only"},),
    )
    memory = write_arm(run_root, replica=1, condition="memory")
    _set_structured_rubric(dataset)
    _set_structured_output(
        memory,
        {
            "response": "Customer-safe note with current-only.",
            "decision": "READY",
            "tier": "SAFE",
        },
    )

    record = build_record(run_root, CASE_ID, dataset)

    by_condition = {item["condition"]: item for item in record["scoring"]}
    assert by_condition["control"]["rubric_provenance"] == "legacy_expected"
    assert by_condition["control"]["passed"] is True
    assert by_condition["memory"]["rubric_provenance"] == "expected"
    assert by_condition["memory"]["passed"] is False
    assert by_condition["memory"]["forbidden"] == ["current-only"]
