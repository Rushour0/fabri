"""Slack install storage; tokens are plaintext-at-rest v1 and never logged."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class SlackInstallStore:
    """Store Slack installs with plaintext-at-rest v1 tokens that are never logged."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS installs(
                    team_id TEXT PRIMARY KEY,
                    bot_token TEXT NOT NULL,
                    team_name TEXT,
                    scopes TEXT,
                    installed_at REAL,
                    updated_at REAL
                )
                """
            )

    def upsert(
        self,
        *,
        team_id: str,
        bot_token: str,
        team_name: str | None = None,
        scopes: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO installs(
                    team_id, bot_token, team_name, scopes, installed_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(team_id) DO UPDATE SET
                    bot_token=excluded.bot_token,
                    team_name=excluded.team_name,
                    scopes=excluded.scopes,
                    updated_at=excluded.updated_at
                """,
                (team_id, bot_token, team_name, scopes, now, now),
            )

    def get_token(self, team_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT bot_token FROM installs WHERE team_id=?", (team_id,)
            ).fetchone()
        return row["bot_token"] if row is not None else None

    def get(self, team_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT team_id, team_name, scopes, installed_at, updated_at
                FROM installs WHERE team_id=?
                """,
                (team_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT team_id, team_name, scopes, installed_at, updated_at
                FROM installs
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, team_id: str) -> bool:
        with self._connect() as connection:
            cur = connection.execute(
                "DELETE FROM installs WHERE team_id=?", (team_id,)
            )
        return cur.rowcount > 0
