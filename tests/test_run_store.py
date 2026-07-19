"""Durable run-index coverage without launching agents or calling a network."""
from __future__ import annotations

import json
from pathlib import Path

from fabri.service.run_store import RunStore
from fabri.service.service import FabriService


def test_insert_update_list_filters_and_pagination(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.record_submit(session_id="one", agency="alpha", task="first", submitted_at=1.0)
    store.record_submit(session_id="two", agency="beta", task="second", submitted_at=2.0)
    store.record_submit(session_id="three", agency="alpha", task="third", submitted_at=3.0)
    store.record_terminal(
        session_id="three", finished_at=4.0, event="result", outcome="success",
        cost={"total_cost_usd": 1.25, "cost_by_model": {"model": 1.25}, "metrics": {"step_count": 3}},
    )

    alpha = store.list_runs(agency="alpha", limit=1, offset=1)
    assert [run["session_id"] for run in alpha] == ["one"]
    updated = store.list_runs(limit=1)[0]
    assert updated["status"] == "done"
    assert updated["cost"] == {
        "total_cost_usd": 1.25,
        "cost_by_model": {"model": 1.25},
        "metrics": {"step_count": 3, "guideline_reuse_rate": None, "guidelines_from_prior_sessions": None},
    }


def test_agency_aggregates_are_oldest_to_newest(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    for session_id, submitted_at, cost, reuse in (("later", 20.0, 2.0, 0.8), ("first", 10.0, 1.0, 0.2)):
        store.record_submit(session_id=session_id, agency="alpha", task=session_id, submitted_at=submitted_at)
        store.record_terminal(
            session_id=session_id, finished_at=submitted_at + 1, event="result", outcome="success",
            cost={"total_cost_usd": cost, "metrics": {"guideline_reuse_rate": reuse}},
        )

    aggregate = store.agency_aggregates()
    assert aggregate == [{
        "agency": "alpha", "run_count": 2, "cost_total": 3.0,
        "cost_per_run_series": [
            {"session_id": "first", "finished_at": 11.0, "cost_total": 1.0},
            {"session_id": "later", "finished_at": 21.0, "cost_total": 2.0},
        ],
        "reuse_series": [
            {"session_id": "first", "finished_at": 11.0, "reuse_rate": 0.2},
            {"session_id": "later", "finished_at": 21.0, "reuse_rate": 0.8},
        ],
    }]


def test_index_jsonl_migrates_when_database_is_empty(tmp_path: Path) -> None:
    index = tmp_path / "index.jsonl"
    index.write_text("\n".join((
        json.dumps({"event": "submit", "session_id": "old", "task": "legacy", "started_ts": 2.0, "agency": "archive"}),
        json.dumps({"event": "result", "session_id": "old", "ts": 5.0, "outcome": "success", "cost": {"total_cost_usd": 0.4, "cost_by_model": {"old-model": 0.4}, "metrics": {"step_count": 2}}}),
    )) + "\n")

    store = RunStore(tmp_path / "runs.db", index_path=index)
    assert store.list_runs() == [{
        "session_id": "old", "agency": "archive", "task": "legacy", "status": "done",
        "outcome": "success", "thread_id": None, "fleet_id": None, "label": None,
        "started_ts": 2.0, "ended_ts": 5.0,
        "cost": {"total_cost_usd": 0.4, "cost_by_model": {"old-model": 0.4}, "metrics": {"step_count": 2, "guideline_reuse_rate": None, "guidelines_from_prior_sessions": None}},
    }]


def test_default_home_is_persistent_unless_ephemeral(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FABRI_HOME", raising=False)
    monkeypatch.delenv("FABRI_EPHEMERAL", raising=False)
    persistent = FabriService()
    assert persistent.home_root == tmp_path / "home" / ".fabri" / "serve"
    assert persistent.home_root.is_dir()
    persistent.close()

    monkeypatch.setenv("FABRI_EPHEMERAL", "1")
    ephemeral = FabriService()
    assert ephemeral.home_root.name.startswith("fabri-serve-")
    assert ephemeral.home_root != persistent.home_root
    ephemeral.close()
