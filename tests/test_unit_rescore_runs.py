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
    from benchmarks import rescore_runs
finally:
    if _previous_local_cost_map is None:
        os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    else:
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = _previous_local_cost_map

from tests.fixtures.benchmark_runs import (
    CASE_ID,
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


def test_main_infers_case_id_from_results_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting --case must use the study metadata's case id."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="memory")
    monkeypatch.setattr(
        "sys.argv",
        ["rescore_runs.py", "--run-root", str(run_root), "--dataset", str(dataset)],
    )

    rescore_runs.main()

    output = capsys.readouterr().out
    assert f"case: {CASE_ID}   arms: 1" in output
    assert "memory" in output
    assert "arms=1" in output
    assert "scored=1" in output
    assert "raw=100.0% (1/1)" in output
    assert "corrected=100.0% (1/1)" in output


def test_main_without_case_or_results_json_exits_with_documented_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with no inferable case must stop with an actionable error."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path, include_results=False)
    monkeypatch.setattr(
        "sys.argv",
        ["rescore_runs.py", "--run-root", str(run_root), "--dataset", str(dataset)],
    )

    with pytest.raises(SystemExit, match="could not infer --case; pass it explicitly"):
        rescore_runs.main()


def test_main_records_unknown_correction_when_final_text_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing FINAL evidence must become an annotated unknown row, not a false failure."""
    dataset = write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(
        run_root,
        replica=1,
        condition="control",
        rubric_passed=None,
        trace_events=({"type": "USAGE", "cost_usd": 1.0},),
    )
    json_out = tmp_path / "missing-final.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "rescore_runs.py",
            "--run-root",
            str(run_root),
            "--dataset",
            str(dataset),
            "--json-out",
            str(json_out),
        ],
    )

    rescore_runs.main()

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["rows"] == [
        {
            "replica": 1,
            "condition": "control",
            "raw_passed": None,
            "corrected_passed": None,
            "scoring_mode": "legacy_prose",
            "rubric_provenance": "legacy_expected",
            "note": "no FINAL text found",
            "cost": 1.0,
        }
    ]
    output = capsys.readouterr().out
    assert "arms=1" in output
    assert "scored=0" in output
    assert "raw=unavailable (0 scored)" in output
    assert "corrected=unavailable (0 scored)" in output
    assert "nan%" not in output.lower()


def test_summary_reports_two_scorable_arms_out_of_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    monkeypatch.setattr(
        "sys.argv",
        ["rescore_runs.py", "--run-root", str(run_root), "--dataset", str(dataset)],
    )

    rescore_runs.main()

    output = capsys.readouterr().out
    assert "memory    arms=5" in output
    assert "scored=2" in output
    assert "raw=100.0% (5/5)" in output
    assert "corrected=50.0% (1/2)" in output


def test_main_corrects_negated_forbidden_phrase_from_raw_fail_to_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Negated deployment language must flip a stale raw failure to corrected pass."""
    dataset = write_dataset(
        tmp_path,
        required=(),
        forbidden=("fix was deployed",),
    )
    run_root = write_run(tmp_path)
    write_arm(
        run_root,
        replica=1,
        condition="memory",
        rubric_passed=False,
        forbidden_hits=("fix was deployed",),
        trace_events=(
            {
                "type": "FINAL",
                "text": "No crew supplied evidence that a corrective fix was deployed.",
            },
        ),
    )
    json_out = tmp_path / "negation.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "rescore_runs.py",
            "--run-root",
            str(run_root),
            "--dataset",
            str(dataset),
            "--json-out",
            str(json_out),
        ],
    )

    rescore_runs.main()

    row = json.loads(json_out.read_text(encoding="utf-8"))["rows"][0]
    assert row["raw_passed"] is False
    assert row["raw_forbidden"] == ["fix was deployed"]
    assert row["corrected_passed"] is True
    assert row["corrected_forbidden"] == []
    assert "YES" in capsys.readouterr().out


def test_json_out_contains_rubric_and_one_row_per_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The audit artifact must preserve case, rubric terms, and every discovered arm."""
    required = [["checkout"], ["rollback", "rolled back"]]
    forbidden = ["blame"]
    dataset = write_dataset(tmp_path, required=required, forbidden=forbidden)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="memory")
    write_arm(run_root, replica=1, condition="control")
    json_out = tmp_path / "rescore.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "rescore_runs.py",
            "--run-root",
            str(run_root),
            "--dataset",
            str(dataset),
            "--case",
            CASE_ID,
            "--json-out",
            str(json_out),
        ],
    )

    assert rescore_runs._rubric_for_case(dataset, CASE_ID) == (
        (("checkout",), ("rollback", "rolled back")),
        {},
        ("blame",),
        (("checkout",), ("rollback", "rolled back")),
        ("blame",),
    )
    rescore_runs.main()

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["case_id"] == CASE_ID
    assert payload["required"] == required
    assert payload["structured"] == {}
    assert payload["forbidden"] == forbidden
    assert payload["legacy_expected"] == {
        "required": required,
        "forbidden": forbidden,
    }
    assert len(payload["rows"]) == 2
    assert {(row["replica"], row["condition"]) for row in payload["rows"]} == {
        (1, "memory"),
        (1, "control"),
    }
    assert f"wrote {json_out}" in capsys.readouterr().out


def test_final_text_uses_result_fallback_and_json_encodes_non_string(
    tmp_path: Path,
) -> None:
    """Legacy result payloads, including structured values, must remain readable."""
    run_root = write_run(tmp_path)
    string_arm = write_arm(
        run_root,
        replica=1,
        condition="memory",
        arm_name="string-result",
        trace_events=({"type": "FINAL", "result": "legacy final"},),
    )
    structured_arm = write_arm(
        run_root,
        replica=2,
        condition="memory",
        arm_name="structured-result",
        trace_events=({"type": "FINAL", "result": {"status": "ready", "count": 2}},),
    )

    assert rescore_runs._final_text(string_arm, "string-result-session") == "legacy final"
    assert rescore_runs._final_text(
        structured_arm, "structured-result-session"
    ) == json.dumps({"status": "ready", "count": 2})


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
def test_main_scores_present_structure_and_designated_response_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    json_out = tmp_path / "structured-rescore.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "rescore_runs.py",
            "--run-root",
            str(run_root),
            "--dataset",
            str(dataset),
            "--json-out",
            str(json_out),
        ],
    )

    rescore_runs.main()

    row = json.loads(json_out.read_text(encoding="utf-8"))["rows"][0]
    assert row["scoring_mode"] == "structured"
    assert row["rubric_provenance"] == "expected"
    assert row["corrected_passed"] is passed
    assert row["corrected_missing"] == missing
    assert row["corrected_forbidden"] == forbidden
    capsys.readouterr()


def test_main_absent_structure_uses_legacy_prose_and_records_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = write_dataset(tmp_path)
    _set_structured_rubric(dataset)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="control")
    json_out = tmp_path / "legacy-rescore.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "rescore_runs.py",
            "--run-root",
            str(run_root),
            "--dataset",
            str(dataset),
            "--json-out",
            str(json_out),
        ],
    )

    rescore_runs.main()

    row = json.loads(json_out.read_text(encoding="utf-8"))["rows"][0]
    assert row["scoring_mode"] == "legacy_prose"
    assert row["rubric_provenance"] == "legacy_expected"
    assert row["corrected_passed"] is True
    assert row["corrected_missing"] == []
    capsys.readouterr()


def test_main_discovers_and_scores_each_case_in_multi_study_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = write_dataset(tmp_path)
    dataset_data = yaml.safe_load(dataset.read_text(encoding="utf-8"))
    dataset_data["cases"].append(
        {
            "id": "second_case",
            "expected": {"required": [["checkout"]], "forbidden": ["blame"]},
            "legacy_expected": {
                "required": [["checkout"]],
                "forbidden": ["blame"],
            },
        }
    )
    dataset.write_text(yaml.safe_dump(dataset_data), encoding="utf-8")
    archive = tmp_path / "archive"
    first = write_run(archive, name="first", case_id=CASE_ID)
    second = write_run(archive, name="second", case_id="second_case")
    write_arm(first, replica=1, condition="memory")
    write_arm(second, replica=1, condition="control")
    json_out = tmp_path / "multi-study.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "rescore_runs.py",
            "--run-root",
            str(archive),
            "--dataset",
            str(dataset),
            "--json-out",
            str(json_out),
        ],
    )

    rescore_runs.main()

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert [case["case_id"] for case in payload["cases"]] == [CASE_ID, "second_case"]
    assert [len(case["rows"]) for case in payload["cases"]] == [1, 1]
    output = capsys.readouterr().out
    assert f"case: {CASE_ID}   arms: 1" in output
    assert "case: second_case   arms: 1" in output


def test_main_fails_loudly_when_legacy_rubric_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _fixture_write_dataset(tmp_path)
    run_root = write_run(tmp_path)
    write_arm(run_root, replica=1, condition="control")
    monkeypatch.setattr(
        "sys.argv",
        ["rescore_runs.py", "--run-root", str(run_root), "--dataset", str(dataset)],
    )

    with pytest.raises(ValueError, match="legacy_expected is required"):
        rescore_runs.main()
