from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.agreement.build_label_sheet import (
    build_label_sheet,
    load_run_logs,
    select_sample,
)
from benchmarks.agreement.score_agreement import cohens_kappa, score_sheet

pytestmark = pytest.mark.unit


def _write_log(
    logs_root: Path,
    *,
    stem: str,
    raw_verdict: bool | None,
    corrected_verdict: bool | None,
    final_output: str = "A neutral synthetic agent response.",
) -> Path:
    company = "support-hq"
    directory = logs_root / "per-run" / company
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.md"

    def rendered(value: object) -> str:
        if value is None:
            return "null"
        return str(value)

    path.write_text(
        "\n".join(
            [
                "| Field | Value |",
                "| --- | --- |",
                f"| company | {company} |",
                "| phase | synthetic-holdout |",
                "| replica / variant | replica-01 / null |",
                "| outcome | success |",
                "| complete | True |",
                "| cost_usd | 0.01 |",
                "| total_retries | 0 |",
                f"| raw verdict | {rendered(raw_verdict)} |",
                f"| corrected verdict | {rendered(corrected_verdict)} |",
                "| negation-FP list | [] |",
                "",
                "## Final output",
                "",
                final_output,
            ]
        ),
        encoding="utf-8",
    )
    return path


def _make_sampling_corpus(logs_root: Path) -> None:
    _write_log(logs_root, stem="flip-fail-to-pass", raw_verdict=False, corrected_verdict=True)
    _write_log(logs_root, stem="flip-pass-to-fail", raw_verdict=True, corrected_verdict=False)
    _write_log(logs_root, stem="corrected-fail", raw_verdict=False, corrected_verdict=False)
    _write_log(logs_root, stem="pass-a", raw_verdict=True, corrected_verdict=True)
    _write_log(logs_root, stem="pass-b", raw_verdict=True, corrected_verdict=True)
    _write_log(logs_root, stem="pass-c", raw_verdict=True, corrected_verdict=True)
    _write_log(logs_root, stem="unscored-training", raw_verdict=None, corrected_verdict=None)


def test_sampling_is_deterministic_and_prioritizes_mandatory_strata(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    _make_sampling_corpus(logs_root)
    runs = load_run_logs(logs_root)

    first = select_sample(runs, sample_size=4, seed=20260722)
    second = select_sample(runs, sample_size=4, seed=20260722)

    assert [run.run_id for run in first.runs] == [run.run_id for run in second.runs]
    assert first.flips == 2
    assert first.corrected_fails == 1
    assert first.random_fill == 1
    assert {run.log_path.stem for run in first.runs} >= {
        "flip-fail-to-pass",
        "flip-pass-to-fail",
        "corrected-fail",
    }
    assert "unscored-training" not in {run.log_path.stem for run in first.runs}

    # Mandatory strata are never truncated, even when they exceed the request.
    oversampled = select_sample(runs, sample_size=1, seed=20260722)
    assert len(oversampled.runs) == 3
    assert oversampled.oversampled


def test_generated_sheet_does_not_leak_hidden_scorer_data(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    source = _write_log(
        logs_root,
        stem="private-source-filename",
        raw_verdict=True,
        corrected_verdict=False,
    )
    # A sibling rubric file is realistic bait: the builder must never source the
    # human-facing statement from these mechanical match strings.
    (logs_root / "rubric_terms.json").write_text(
        json.dumps(
            {
                "support_hq_safe_incident_response": {
                    "required": ["secret-required-token"],
                    "forbidden": ["secret-forbidden-token"],
                }
            }
        ),
        encoding="utf-8",
    )
    sheet_path = tmp_path / "sheet.md"
    key_path = tmp_path / "key.json"

    build_label_sheet(
        logs_root=logs_root,
        sample_size=1,
        seed=7,
        out_sheet=sheet_path,
        out_key=key_path,
    )
    sheet = sheet_path.read_text(encoding="utf-8")

    assert "raw verdict" not in sheet
    assert "corrected verdict" not in sheet
    assert "True" not in sheet
    assert "False" not in sheet
    assert "secret-required-token" not in sheet
    assert "secret-forbidden-token" not in sheet
    assert source.name not in sheet
    assert "support-hq" not in sheet


def test_sheet_build_fill_and_score_round_trip(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    _write_log(logs_root, stem="one", raw_verdict=True, corrected_verdict=True)
    _write_log(logs_root, stem="two", raw_verdict=False, corrected_verdict=True)
    _write_log(logs_root, stem="three", raw_verdict=False, corrected_verdict=False)
    _write_log(logs_root, stem="four", raw_verdict=True, corrected_verdict=True)
    sheet_path = tmp_path / "sheet.md"
    key_path = tmp_path / "key.json"
    build_label_sheet(
        logs_root=logs_root,
        sample_size=4,
        seed=11,
        out_sheet=sheet_path,
        out_key=key_path,
    )

    key = json.loads(key_path.read_text(encoding="utf-8"))
    human_by_item = {
        item["item"]: ("pass" if item["raw_verdict"] else "fail")
        for item in key["items"]
    }
    current_item = 0
    filled_lines = []
    for line in sheet_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Item "):
            current_item = int(line.removeprefix("## Item "))
        if line == "verdict:":
            line = f"verdict: {human_by_item[current_item]}"
        elif line == "reason:":
            line = "reason: checked against the plain-language intent"
        filled_lines.append(line)
    sheet_path.write_text("\n".join(filled_lines) + "\n", encoding="utf-8")

    report = score_sheet(sheet_path, key_path)

    assert report["item_count"] == 4
    assert report["raw"]["agreement_percent"] == 100.0
    assert report["raw"]["disagreements"] == []
    assert report["corrected"]["agreement_count"] == 3
    assert report["corrected"]["disagreements"][0]["reason"] == (
        "checked against the plain-language intent"
    )


def test_cohens_kappa_matches_hand_computed_fixture() -> None:
    human = [True, True, True, False, False, False]
    scorer = [True, True, False, True, False, False]

    # Observed agreement = 4/6; both pass marginals = 3/6, so expected
    # agreement = .5*.5 + .5*.5 = .5. Kappa = (4/6 - .5) / (1 - .5) = 1/3.
    assert cohens_kappa(human, scorer) == pytest.approx(1 / 3)


def test_cohens_kappa_is_one_for_perfect_mixed_class_agreement() -> None:
    assert cohens_kappa([True, False, True, False], [True, False, True, False]) == 1.0


def test_cohens_kappa_is_zero_at_chance_level() -> None:
    # Both raters are 50/50 and agree on 2/4 items: observed equals expected.
    assert cohens_kappa([True, True, False, False], [True, False, True, False]) == pytest.approx(0.0)


def test_cohens_kappa_is_undefined_without_label_variance() -> None:
    assert cohens_kappa([True, True], [True, True]) is None
    assert cohens_kappa([True, False], [True, True]) is None


def test_unfilled_verdict_names_the_offending_item(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    _write_log(logs_root, stem="one", raw_verdict=True, corrected_verdict=True)
    sheet_path = tmp_path / "sheet.md"
    key_path = tmp_path / "key.json"
    build_label_sheet(
        logs_root=logs_root,
        sample_size=1,
        seed=1,
        out_sheet=sheet_path,
        out_key=key_path,
    )

    with pytest.raises(ValueError, match=r"unfilled verdicts for items: 1"):
        score_sheet(sheet_path, key_path)
