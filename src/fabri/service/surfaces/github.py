"""GitHub as a surface adapter.

An issue or pull-request comment starting with ``/fabri run <ref> <task>`` runs
that catalog entry and posts the result back as a comment on the same thread.

Two things differ from Slack and are worth naming, because they are the reasons
the contract looks the way it does:

* Credentials are minted per delivery. Slack and Linear hold long-lived tokens;
  a GitHub App mints a short-lived installation token, keyed by the installation
  id in the *verified* payload — never by a lookup that could cross tenants.
* There is no way to ask a human a question mid-run, so ``hitl`` is False and
  the pipeline disables ``ask_user`` for these runs. Comment-thread questions
  are a real feature, but a run that stops to ask with nowhere to ask would
  block until the process died.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from fabri.service import github_app
from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.pipeline import parse_command
from fabri.service.surfaces.types import (
    Dispatch,
    Handled,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    SurfaceCapabilities,
    TenantRef,
)

_LOG = logging.getLogger("fabri")

#: A command has to be unmistakable in a comment thread full of prose.
COMMAND_PREFIX = "/fabri"


class GitHubAdapter(SurfaceAdapter):
    name = "github"
    webhook_path = "/github/webhook"

    def __init__(self, service: Any = None) -> None:
        self.service = service

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(hitl=False, threads=True)

    # --- inbound ------------------------------------------------------------

    def verify(self, raw_body: bytes, headers: Mapping[str, str]):
        secret = os.environ.get("GITHUB_APP_WEBHOOK_SECRET")
        signature = headers.get("X-Hub-Signature-256", "")
        if not secret or not github_app.verify_webhook_signature(
            secret, raw_body, signature
        ):
            return ShortCircuit(401, "")
        try:
            payload = json.loads(raw_body or b"{}")
        except (TypeError, ValueError):
            return ShortCircuit(400, "")
        if not isinstance(payload, dict):
            return ShortCircuit(400, "")
        # The event name lives in a header, and classify() only sees the
        # payload; carry it across rather than widening the contract for one
        # surface's transport detail.
        payload["_event"] = headers.get("X-GitHub-Event", "")
        payload["_delivery"] = headers.get("X-GitHub-Delivery", "")
        return payload

    def delivery_id(self, payload: dict) -> str | None:
        delivery = payload.get("_delivery")
        return delivery if isinstance(delivery, str) and delivery else None

    def classify(self, payload: dict):
        event = payload.get("_event", "")
        if event in ("installation", "installation_repositories"):
            self._handle_install_lifecycle(event, payload)
            return Handled()
        if event != "issue_comment" or payload.get("action") != "created":
            return Ignore()

        comment = payload.get("comment") or {}
        # Never act on our own comments: a result that triggers a run is an
        # infinite loop with a bill attached.
        if (comment.get("user") or {}).get("type") == "Bot":
            return Ignore()

        body = comment.get("body")
        if not isinstance(body, str) or not body.strip().startswith(COMMAND_PREFIX):
            return Ignore()
        text = body.strip()[len(COMMAND_PREFIX) :].strip()

        repo = (payload.get("repository") or {}).get("full_name")
        number = (payload.get("issue") or {}).get("number")
        installation_id = (payload.get("installation") or {}).get("id")
        if not repo or number is None:
            return Ignore()

        command = parse_command(text)
        if command is None:
            return Ignore()
        target = ReplyTarget(
            TenantRef(self.name, str(installation_id) if installation_id else None),
            {"repo": repo, "issue_number": number},
        )
        return Dispatch(command, target)

    def _handle_install_lifecycle(self, event: str, payload: dict) -> None:
        """Installation bookkeeping, unchanged from the pre-surfaces handler."""
        from fabri.service.github_events import apply_install_lifecycle

        if self.service is not None:
            apply_install_lifecycle(event, payload, self.service)

    # --- outbound -----------------------------------------------------------

    def _token(self, target: ReplyTarget) -> str | None:
        """A short-lived installation token for this delivery's installation."""
        installation_id = target.tenant.tenant_id
        if not installation_id:
            return None
        try:
            from fabri.repo.github_auth import AppAuth

            return AppAuth(installation_id=installation_id).get_token()
        except Exception:
            _LOG.exception("could not mint a GitHub installation token")
            return None

    def _comment(self, target: ReplyTarget, body: str) -> bool:
        token = self._token(target)
        repo = target.locator.get("repo")
        number = target.locator.get("issue_number")
        if not token or not repo or number is None:
            return False
        try:
            from fabri.repo.github import comment_issue

            comment_issue(repo, token, int(number), body)
            return True
        except Exception:
            _LOG.exception("could not comment on GitHub")
            return False

    def deliver_ack(self, target: ReplyTarget, text: str) -> bool:
        return self._comment(target, text)

    def deliver_result(self, target: ReplyTarget, outcome: RunOutcome) -> bool:
        # Agent output is fenced: an issue body is Markdown, and an unfenced
        # result could @-mention half an organisation on its way past.
        lines = ["```", outcome.final_text, "```"]
        if outcome.total_cost_usd is not None:
            lines.append(f"_Cost: ${outcome.total_cost_usd:.4f}_")
        return self._comment(target, "\n".join(lines))

    def deliver_error(self, target: ReplyTarget, text: str) -> bool:
        return self._comment(target, text)
