from fabri.benchmarks.openai_recovery_study import (
    HOLDOUT_TASK,
    TASK,
    grade_holdout_summary,
    grade_summary,
    summarize,
    task_for_session,
)
from fabri.events import EventType
from fabri.orchestrator.pipeline import file_recovery_evidence


def test_grade_summary_requires_every_agenda_item(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "agenda_summary.txt").write_text(
        "Confirm who owns the incident follow-up. Publish the release notes."
    )

    assert grade_summary(tmp_path)


def test_grade_holdout_summary_requires_every_brief_item(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "brief_summary.txt").write_text(
        "The rollback owner must be confirmed. Send the customer update today."
    )

    assert grade_holdout_summary(tmp_path)


def test_task_for_session_uses_training_then_holdout():
    assert task_for_session(1) == ("training", TASK)
    assert task_for_session(2) == ("holdout", HOLDOUT_TASK)


def test_summarize_reports_rubric_and_cost_change():
    replicas = [
        {"runs": [
            {"cost_usd": 0.02, "rubric_passed": True},
            {"cost_usd": 0.01, "rubric_passed": True},
        ]},
        {"runs": [
            {"cost_usd": 0.04, "rubric_passed": True},
            {"cost_usd": 0.02, "rubric_passed": False},
        ]},
    ]

    assert summarize(replicas) == {
        "replicas": 2,
        "sessions_per_replica": 2,
        "rubric_pass_rate": 0.75,
        "first_session_mean_cost_usd": 0.03,
        "final_session_mean_cost_usd": 0.015,
        "mean_cost_change_pct": -50.0,
    }


def test_file_recovery_evidence_requires_listing_and_verified_alternative():
    events = [
        {"type": EventType.TOOL_CALL.value, "name": "read_file", "args": {"path": "notes/agenda.txt"}, "result": {"ok": False}},
        {"type": EventType.TOOL_CALL.value, "name": "list_dir", "args": {"path": "notes"}, "result": {"ok": True}},
        {"type": EventType.TOOL_CALL.value, "name": "read_file", "args": {"path": "notes/agenda.md"}, "result": {"ok": True}},
    ]

    assert file_recovery_evidence(events) == [
        "Recovery observed: after an exact file read failed, listing its parent "
        "and verifying an alternate file allowed the task to continue."
    ]
