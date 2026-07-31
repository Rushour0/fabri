"""Slack install storage; tokens are plaintext-at-rest v1 and never logged."""
from __future__ import annotations

import json
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


class GitHubInstallStore:
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
                CREATE TABLE IF NOT EXISTS github_installs(
                    installation_id TEXT PRIMARY KEY,
                    account_login TEXT,
                    account_type TEXT,
                    repos TEXT,
                    installed_at REAL,
                    updated_at REAL
                )
                """
            )

    def upsert(
        self,
        *,
        installation_id: str,
        account_login: str | None = None,
        account_type: str | None = None,
        repos: list[str] | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        repos_json = json.dumps(repos) if repos is not None else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO github_installs(
                    installation_id, account_login, account_type, repos,
                    installed_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(installation_id) DO UPDATE SET
                    account_login=COALESCE(
                        excluded.account_login, github_installs.account_login
                    ),
                    account_type=COALESCE(
                        excluded.account_type, github_installs.account_type
                    ),
                    repos=COALESCE(excluded.repos, github_installs.repos),
                    updated_at=excluded.updated_at
                """,
                (
                    installation_id,
                    account_login,
                    account_type,
                    repos_json,
                    now,
                    now,
                ),
            )

    def get(self, installation_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT installation_id, account_login, account_type, repos,
                    installed_at, updated_at
                FROM github_installs WHERE installation_id=?
                """,
                (installation_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["repos"] = (
            json.loads(row["repos"]) if row["repos"] is not None else None
        )
        return result

    def get_installation(self, account_or_repo: str) -> str | None:
        owner = (
            account_or_repo.split("/")[0]
            if "/" in account_or_repo
            else account_or_repo
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT installation_id FROM github_installs
                WHERE account_login=?
                """,
                (owner,),
            ).fetchone()
            if row is not None:
                return row["installation_id"]
            rows = connection.execute(
                "SELECT installation_id, repos FROM github_installs"
            ).fetchall()
        for row in rows:
            if row["repos"] is not None:
                if account_or_repo in json.loads(row["repos"]):
                    return row["installation_id"]
        return None

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT installation_id, account_login, account_type, repos,
                    installed_at, updated_at
                FROM github_installs
                """
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["repos"] = (
                json.loads(row["repos"]) if row["repos"] is not None else None
            )
            results.append(result)
        return results

    def delete(self, installation_id: str) -> bool:
        with self._connect() as connection:
            cur = connection.execute(
                "DELETE FROM github_installs WHERE installation_id=?",
                (installation_id,),
            )
        return cur.rowcount > 0


class LinearInstallStore:
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
                CREATE TABLE IF NOT EXISTS linear_installs(
                    workspace_id TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    workspace_name TEXT,
                    scopes TEXT,
                    installed_at REAL,
                    updated_at REAL
                )
                """
            )

    def upsert(
        self,
        *,
        workspace_id: str,
        access_token: str,
        workspace_name: str | None = None,
        scopes: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO linear_installs(
                    workspace_id, access_token, workspace_name, scopes,
                    installed_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    access_token=excluded.access_token,
                    workspace_name=excluded.workspace_name,
                    scopes=excluded.scopes,
                    updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    access_token,
                    workspace_name,
                    scopes,
                    now,
                    now,
                ),
            )

    def get_token(self, workspace_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT access_token FROM linear_installs WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return row["access_token"] if row is not None else None

    def get(self, workspace_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workspace_id, workspace_name, scopes,
                    installed_at, updated_at
                FROM linear_installs WHERE workspace_id=?
                """,
                (workspace_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT workspace_id, workspace_name, scopes,
                    installed_at, updated_at
                FROM linear_installs
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, workspace_id: str) -> bool:
        with self._connect() as connection:
            cur = connection.execute(
                "DELETE FROM linear_installs WHERE workspace_id=?",
                (workspace_id,),
            )
        return cur.rowcount > 0
