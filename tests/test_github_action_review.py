"""Unit tests for the pure GitHub Action review helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

MODULE_PATH = Path(__file__).parents[1] / "examples/github-action/review.py"
SPEC = importlib.util.spec_from_file_location("github_action_review", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def test_build_task_includes_pull_request_context_and_diff() -> None:
    diff = "@@ -1 +1 @@\n-old\n+new"

    task = review.build_task(diff, "Fix pagination", 42)

    assert "#42" in task
    assert "Fix pagination" in task
    assert diff in task


def test_parse_run_output_extracts_structured_output() -> None:
    stdout = 'fabri progress\n{"final_text":"done","structured_output":{"verdict":"approve","summary":"Looks good","findings":[]}}\n'

    assert review.parse_run_output(stdout) == {
        "verdict": "approve",
        "summary": "Looks good",
        "findings": [],
    }


@pytest.mark.parametrize("stdout", ["not json", '{"final_text":"missing schema"}'])
def test_parse_run_output_rejects_malformed_or_missing_structured_output(stdout: str) -> None:
    with pytest.raises(ValueError, match="structured_output"):
        review.parse_run_output(stdout)


def test_format_comment_renders_grouped_findings_table() -> None:
    comment = review.format_comment(
        {
            "verdict": "request_changes",
            "summary": "One boundary error needs correction.",
            "findings": [
                {"file": "src/pagination.py", "line": 4, "severity": "blocker", "note": "End excludes an item."},
                {"file": "src/style.py", "line": None, "severity": "nit", "note": "Use a clearer name."},
            ],
        }
    )

    assert "Fabri code review — Request Changes" in comment
    assert "One boundary error" in comment
    assert "### Blocker findings" in comment
    assert "### Nit findings" in comment
    assert "| File | Line | Finding |" in comment
    assert "`src/pagination.py`" in comment


def test_format_comment_handles_empty_findings() -> None:
    comment = review.format_comment({"verdict": "approve", "summary": "Looks good.", "findings": []})

    assert "Approve" in comment
    assert "No findings. Nice work!" in comment


def test_exit_code_for_matches_or_exceeds_failure_threshold() -> None:
    assert review.exit_code_for({"verdict": "request_changes"}, "request_changes") == 1
    assert review.exit_code_for({"verdict": "request_changes"}, "comment") == 1
    assert review.exit_code_for({"verdict": "comment"}, "request_changes") == 0
    assert review.exit_code_for({"verdict": "approve"}, "comment") == 0
