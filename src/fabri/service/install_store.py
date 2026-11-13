"""Workspace install stores for Slack, GitHub and Linear.

Slack and Linear tokens live in the ``connections`` table
(``fabri.service.connection_store``); the two classes here keep their old
names, signatures and row shapes as thin wrappers over ``ConnectionStore``.
Secrets at rest are encrypted when FABRI_SECRET_KEY is set and plaintext in
the 0600 file otherwise (see ``fabri.secret_cipher``), and no token is ever
logged. ``GitHubInstallStore`` holds no token and keeps its own table.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from fabri.service.connection_store import REFRESH_MARGIN_S, ConnectionStore

__all__ = [
    "REFRESH_MARGIN_S",
    "GitHubInstallStore",
    "LinearInstallStore",
    "SlackInstallStore",
]


class SlackInstallStore:
    """Slack workspace installs, stored as ``slack`` connections keyed by team id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cs = ConnectionStore(path)

    @staticmethod
    def _legacy(row: dict) -> dict:
        return {
            "team_id": row["handle"],
            "team_name": row["display_name"],
            "scopes": row["scopes"],
            "installed_at": row["installed_at"],
            "updated_at": row["updated_at"],
        }

    def upsert(
        self,
        *,
        team_id: str,
        bot_token: str,
        team_name: str | None = None,
        scopes: str | None = None,
        now: float | None = None,
    ) -> None:
        self._cs.upsert(
            provider="slack",
            handle=team_id,
            access_token=bot_token,
            display_name=team_name,
            scopes=scopes,
            now=now,
        )

    def get_token(self, team_id: str) -> str | None:
        return self._cs.get_token("slack", team_id)

    def get(self, team_id: str) -> dict | None:
        row = self._cs.get("slack", team_id)
        return self._legacy(row) if row is not None else None

    def list(self) -> list[dict]:
        return [self._legacy(row) for row in self._cs.list("slack")]

    def delete(self, team_id: str) -> bool:
        return self._cs.delete("slack", team_id)


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
    """Linear workspace installs, stored as ``linear`` connections.

    Linear access tokens last 24 hours and come with a rotating refresh
    token. With a `refresher` (a callable taking the refresh token and
    returning Linear's token response), get_token swaps in a new access token
    when the stored one is within REFRESH_MARGIN_S of expiring.
    """

    def __init__(self, path: Path, refresher: Callable[[str], dict] | None = None) -> None:
        self.refresher = refresher
        self.path = path
        self._cs = ConnectionStore(
            path, refreshers={"linear": refresher} if refresher else None
        )

    @staticmethod
    def _legacy(row: dict) -> dict:
        return {
            "workspace_id": row["handle"],
            "workspace_name": row["display_name"],
            "scopes": row["scopes"],
            "installed_at": row["installed_at"],
            "updated_at": row["updated_at"],
        }

    def upsert(
        self,
        *,
        workspace_id: str,
        access_token: str,
        workspace_name: str | None = None,
        scopes: str | None = None,
        refresh_token: str | None = None,
        expires_in: float | None = None,
        now: float | None = None,
    ) -> None:
        self._cs.upsert(
            provider="linear",
            handle=workspace_id,
            access_token=access_token,
            display_name=workspace_name,
            scopes=scopes,
            refresh_token=refresh_token,
            expires_in=expires_in,
            now=now,
        )

    def get_token(self, workspace_id: str, *, now: float | None = None) -> str | None:
        """The workspace's access token, refreshed first when it is about to
        expire. None when there is no install, or when the token has expired
        and could not be refreshed."""
        return self._cs.get_token("linear", workspace_id, now=now)

    def get(self, workspace_id: str) -> dict | None:
        row = self._cs.get("linear", workspace_id)
        return self._legacy(row) if row is not None else None

    def list(self) -> list[dict]:
        return [self._legacy(row) for row in self._cs.list("linear")]

    def delete(self, workspace_id: str) -> bool:
        return self._cs.delete("linear", workspace_id)
