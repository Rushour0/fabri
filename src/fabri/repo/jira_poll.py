"""Durable, at-most-once polling for Jira resolver label events."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fabri.repo.jira_issue import (
    JiraClient,
    RouteConfig,
    added_by_assignee,
    build_jql,
    latest_label_add,
    scoped_comment_approver,
)
from fabri.repo.jira_run import run_jira_flow
from fabri.tools.registry import Approver

_LOG = logging.getLogger("fabri")


class PollState:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as database:
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS jira_poll_events (
                    issue_key TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    pr_url TEXT,
                    claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT,
                    PRIMARY KEY (issue_key, event_id)
                )
                """
            )

    def claim(self, key: str, event_id: str) -> bool:
        with sqlite3.connect(self.path) as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO jira_poll_events (issue_key, event_id) VALUES (?, ?)",
                (key, event_id),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        key: str,
        event_id: str,
        status: str,
        pr_url: str | None = None,
    ) -> None:
        with sqlite3.connect(self.path) as database:
            database.execute(
                """
                UPDATE jira_poll_events
                SET status = ?, pr_url = ?, finished_at = CURRENT_TIMESTAMP
                WHERE issue_key = ? AND event_id = ?
                """,
                (status, pr_url, key, event_id),
            )


Runner = Callable[..., dict[str, object]]
ApproverFactory = Callable[[str], Approver]


def poll_once(
    routes: RouteConfig,
    *,
    client: JiraClient,
    state: PollState,
    runner: Runner,
    approver_factory: ApproverFactory = scoped_comment_approver,
    log: logging.Logger = _LOG,
    dry_run: bool = False,
) -> list[dict]:
    """Poll every configured project and process eligible label additions serially."""

    records: list[dict] = []
    fields = ["summary", "labels", "assignee", "creator", "created"]
    for project, route in routes.projects.items():
        try:
            issues = client.search(build_jql(project, routes.label), fields)
        except Exception as exc:
            log.exception("Jira search failed for project %s", project)
            records.append(
                {
                    "key": None,
                    "project": project,
                    "event_id": None,
                    "action": "error",
                    "reason": f"search failed: {type(exc).__name__}: {exc}",
                    "ok": False,
                }
            )
            continue
        for issue in issues:
            key = issue.get("key")
            if not isinstance(key, str) or not key:
                log.warning("Jira search returned an issue without a key")
                continue
            try:
                histories = client.get_changelog(key)
                event = latest_label_add(histories, routes.label, issue)
                if event is None:
                    reason = f"label {routes.label!r} is not on the issue"
                    log.info("skipping %s: %s", key, reason)
                    records.append(
                        {
                            "key": key,
                            "event_id": None,
                            "action": "skipped",
                            "reason": reason,
                            "ok": False,
                        }
                    )
                    continue
                allowed, reason = added_by_assignee(event, issue)
                if not allowed:
                    log.info("skipping %s: %s", key, reason)
                    records.append(
                        {
                            "key": key,
                            "event_id": event.event_id,
                            "action": "skipped",
                            "reason": reason,
                            "ok": False,
                        }
                    )
                    continue
                if dry_run:
                    records.append(
                        {
                            "key": key,
                            "event_id": event.event_id,
                            "action": "would_start",
                            "reason": "dry run: nothing claimed or run",
                            "ok": True,
                        }
                    )
                    continue
                if not state.claim(key, event.event_id):
                    records.append(
                        {
                            "key": key,
                            "event_id": event.event_id,
                            "action": "seen",
                            "reason": "label event was already claimed",
                            "ok": True,
                        }
                    )
                    continue
                try:
                    result = runner(
                        issue_key=key,
                        route=route,
                        approver=approver_factory(key),
                    )
                    ok = result.get("ok") is True
                    pr_url_value = result.get("pr_url")
                    pr_url = pr_url_value if isinstance(pr_url_value, str) else None
                    state.finish(key, event.event_id, "succeeded" if ok else "failed", pr_url)
                    records.append(
                        {
                            "key": key,
                            "event_id": event.event_id,
                            "action": "started",
                            "reason": "run completed" if ok else "run failed",
                            "ok": ok,
                        }
                    )
                except Exception as exc:
                    log.exception("Jira resolver run failed for %s", key)
                    state.finish(key, event.event_id, "failed")
                    records.append(
                        {
                            "key": key,
                            "event_id": event.event_id,
                            "action": "started",
                            "reason": f"runner raised {type(exc).__name__}: {exc}",
                            "ok": False,
                        }
                    )
            except Exception as exc:
                log.exception("could not inspect Jira issue %s", key)
                records.append(
                    {
                        "key": key,
                        "event_id": None,
                        "action": "skipped",
                        "reason": f"inspection failed: {type(exc).__name__}: {exc}",
                        "ok": False,
                    }
                )
    return records


def _fabri_home() -> Path:
    return Path(os.environ.get("FABRI_HOME") or Path.home() / ".fabri")


def _poll_dependencies(routes: RouteConfig) -> dict[str, Any]:
    """Build runtime dependencies. Tests replace this to keep polling local."""

    client = JiraClient(os.environ.get("JIRA_URL", ""))
    state = PollState(_fabri_home() / "jira" / "poll.db")

    def runner(**kwargs: object) -> dict[str, object]:
        return run_jira_flow(
            jira=client,
            workspace=_fabri_home() / "jira" / "runs",
            **kwargs,
        )

    return {"client": client, "state": state, "runner": runner, "sleep": time.sleep}
