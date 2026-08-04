"""Durable SQLite index for service-run history."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TypeAlias

RunRecord: TypeAlias = dict[str, object]

_SUCCESS_OUTCOMES = frozenset({"success", "success_with_recovery"})


class RunStore:
    """Small queryable run index stored at ``home_root/runs.db``."""

    def __init__(self, path: Path, *, index_path: Path | None = None, default_agency: str = "default") -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if index_path is not None and self.is_empty() and index_path.exists():
            self._migrate_index(index_path, default_agency)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    session_id TEXT PRIMARY KEY,
                    agency TEXT,
                    task TEXT,
                    submitted_at REAL,
                    finished_at REAL,
                    outcome TEXT,
                    cost_total REAL,
                    cost_by_model TEXT,
                    step_count INTEGER,
                    reuse_rate REAL,
                    guidelines_from_prior INTEGER,
                    thread_id TEXT,
                    fleet_id TEXT,
                    label TEXT,
                    user_id TEXT,
                    terminal_event TEXT,
                    origin TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS runs_agency_submitted ON runs(agency, submitted_at)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            if "user_id" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN user_id TEXT")
            # Which surface asked for the run (JSON), for existing databases.
            if "origin" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN origin TEXT")

    def is_empty(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT NOT EXISTS(SELECT 1 FROM runs)").fetchone()[0] == 1

    def record_submit(
        self, *, session_id: str, agency: str, task: str, submitted_at: float,
        thread_id: str | None = None, fleet_id: str | None = None, label: str | None = None,
        user_id: str | None = None,
        origin: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO runs
                (session_id, agency, task, submitted_at, thread_id, fleet_id, label, user_id, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, agency, task, submitted_at, thread_id, fleet_id, label, user_id, origin),
            )

    def record_terminal(
        self, *, session_id: str, finished_at: float, event: str,
        outcome: str | None, cost: dict | None = None,
    ) -> None:
        metrics = (cost or {}).get("metrics") or {}
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET finished_at = ?, outcome = ?, cost_total = ?,
                cost_by_model = ?, step_count = ?, reuse_rate = ?,
                guidelines_from_prior = ?, terminal_event = ? WHERE session_id = ?""",
                (
                    finished_at,
                    outcome,
                    (cost or {}).get("total_cost_usd"),
                    json.dumps((cost or {}).get("cost_by_model") or {}),
                    metrics.get("step_count"),
                    metrics.get("guideline_reuse_rate"),
                    metrics.get("guidelines_from_prior_sessions"),
                    event,
                    session_id,
                ),
            )

    def list_runs(
        self, *, agency: str | None = None, limit: int | None = None, offset: int = 0,
        user_id: str | None = None,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if agency is not None:
            clauses.append("agency = ?")
            params.append(agency)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        query = "SELECT * FROM runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY submitted_at DESC, session_id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        elif offset:
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)
        with self._connect() as connection:
            return [self._summary(row) for row in connection.execute(query, params)]

    def run_owner(self, session_id: str) -> str | None:
        """Return a run's persisted owner, if it exists and has one."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM runs WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row["user_id"] if row is not None else None

    def agency_aggregates(self, *, user_id: str | None = None) -> list[RunRecord]:
        clauses = " WHERE user_id = ?" if user_id is not None else ""
        params: tuple[object, ...] = (user_id,) if user_id is not None else ()
        with self._connect() as connection:
            agencies = connection.execute(
                "SELECT agency, COUNT(*) AS run_count, COALESCE(SUM(cost_total), 0) AS cost_total "
                f"FROM runs{clauses} GROUP BY agency ORDER BY agency",
                params,
            ).fetchall()
            output: list[RunRecord] = []
            for aggregate in agencies:
                run_params: tuple[object, ...] = (aggregate["agency"],)
                owner_clause = ""
                if user_id is not None:
                    owner_clause = " AND user_id = ?"
                    run_params += (user_id,)
                runs = connection.execute(
                    """SELECT session_id, finished_at, cost_total, reuse_rate FROM runs
                    WHERE agency = ?""" + owner_clause + """
                    ORDER BY finished_at IS NULL, finished_at ASC, submitted_at ASC, session_id ASC""",
                    run_params,
                ).fetchall()
                output.append({
                    "agency": aggregate["agency"],
                    "run_count": aggregate["run_count"],
                    "cost_total": aggregate["cost_total"],
                    "cost_per_run_series": [
                        {"session_id": row["session_id"], "finished_at": row["finished_at"], "cost_total": row["cost_total"]}
                        for row in runs
                    ],
                    "reuse_series": [
                        {"session_id": row["session_id"], "finished_at": row["finished_at"], "reuse_rate": row["reuse_rate"]}
                        for row in runs
                    ],
                })
            return output

    @staticmethod
    def _summary(row: sqlite3.Row) -> RunRecord:
        event = row["terminal_event"]
        outcome = row["outcome"]
        status = "running"
        if event == "cancel":
            status = "cancelled"
        elif event is not None:
            status = "done" if outcome in _SUCCESS_OUTCOMES else "error"
        result: RunRecord = {
            "session_id": row["session_id"], "agency": row["agency"], "task": row["task"],
            "status": status, "outcome": outcome, "thread_id": row["thread_id"],
            "fleet_id": row["fleet_id"], "label": row["label"],
            "started_ts": row["submitted_at"], "ended_ts": row["finished_at"],
        }
        if event != "cancel" and event is not None:
            result["cost"] = {
                "total_cost_usd": row["cost_total"] or 0.0,
                "cost_by_model": json.loads(row["cost_by_model"] or "{}"),
                "metrics": {
                    "step_count": row["step_count"], "guideline_reuse_rate": row["reuse_rate"],
                    "guidelines_from_prior_sessions": row["guidelines_from_prior"],
                },
            }
        return result

    def _migrate_index(self, index_path: Path, default_agency: str) -> None:
        records: dict[str, dict[str, object]] = {}
        for line in index_path.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = record.get("session_id")
            if not session_id:
                continue
            records.setdefault(session_id, {}).update(record)
            if record.get("event") == "submit":
                self.record_submit(
                    session_id=session_id, agency=record.get("agency") or default_agency,
                    task=record.get("task") or "", submitted_at=record.get("started_ts") or record.get("ts") or 0,
                    thread_id=record.get("thread_id"), fleet_id=record.get("fleet_id"), label=record.get("label"),
                    user_id=record.get("user_id"),
                )
            elif record.get("event") in {"result", "cancel"}:
                self.record_terminal(
                    session_id=session_id, finished_at=record.get("ts") or 0,
                    event=record["event"], outcome=record.get("outcome"), cost=record.get("cost"),
                )
