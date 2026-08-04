"""Linear as a surface adapter.

Label an issue ``fabri:<ref>`` and that catalog entry runs against the issue,
reporting back as a comment on the same issue. A label is the trigger rather
than a comment command because it is explicit, visible in the issue, and cannot
be typed by accident in the middle of a discussion.

Webhook secret: Linear signs deliveries with ``Linear-Signature`` (HMAC-SHA256
of the raw body). The secret is read from ``LINEAR_WEBHOOK_SECRET`` — the
webhook is created by hand in Linear's settings today. Creating it automatically
at OAuth time is worth doing, but it is a separate change and this adapter
should not wait on it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.types import (
    Command,
    Dispatch,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    SurfaceCapabilities,
    TenantRef,
)

_LOG = logging.getLogger("fabri")

#: ``fabri:release-notes`` on an issue runs the release-notes entry against it.
LABEL_PREFIX = "fabri:"
_REPLAY_WINDOW_MS = 60_000


def verify_linear_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the raw body, compared in constant time."""
    if not signature:
        return False
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


class LinearAdapter(SurfaceAdapter):
    name = "linear"
    webhook_path = "/linear/webhook"

    def __init__(self, service: Any = None) -> None:
        self.service = service

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(hitl=False, threads=True)

    # --- inbound ------------------------------------------------------------

    def verify(self, raw_body: bytes, headers: Mapping[str, str]):
        secret = os.environ.get("LINEAR_WEBHOOK_SECRET")
        signature = headers.get("Linear-Signature", "")
        if not secret or not verify_linear_signature(secret, raw_body, signature):
            return ShortCircuit(401, "invalid signature")
        try:
            payload = json.loads(raw_body or b"{}")
        except (TypeError, ValueError):
            return ShortCircuit(400, "invalid request JSON")
        if not isinstance(payload, dict):
            return ShortCircuit(400, "invalid request JSON")
        # Linear puts its own timestamp in the body; a delivery replayed later
        # must not start a second run.
        sent_at = payload.get("webhookTimestamp")
        if isinstance(sent_at, (int, float)):
            if abs(time.time() * 1000 - float(sent_at)) > _REPLAY_WINDOW_MS:
                return ShortCircuit(401, "stale delivery")
        return payload

    def delivery_id(self, payload: dict) -> str | None:
        delivery = payload.get("webhookId") or payload.get("deliveryId")
        if isinstance(delivery, str) and delivery:
            timestamp = payload.get("webhookTimestamp")
            return f"{delivery}:{timestamp}"
        return None

    def classify(self, payload: dict):
        if payload.get("type") != "Issue":
            return Ignore()
        if payload.get("action") not in ("create", "update"):
            return Ignore()

        data = payload.get("data") or {}
        issue_id = data.get("id")
        if not isinstance(issue_id, str) or not issue_id:
            return Ignore()

        ref = self._ref_from_labels(data)
        if ref is None:
            return Ignore()

        target = ReplyTarget(
            TenantRef(self.name, payload.get("organizationId")),
            {"issue_id": issue_id, "identifier": data.get("identifier")},
        )
        return Dispatch(Command(verb="run", task=self._task(data), catalog_ref=ref), target)

    def _ref_from_labels(self, data: dict) -> str | None:
        for label in data.get("labels") or []:
            name = label.get("name") if isinstance(label, Mapping) else None
            if isinstance(name, str) and name.startswith(LABEL_PREFIX):
                ref = name[len(LABEL_PREFIX) :].strip()
                if ref:
                    return ref
        return None

    def _task(self, data: dict) -> str:
        """The issue itself is the task: its title, and its body for context."""
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        identifier = str(data.get("identifier") or "").strip()
        header = f"{identifier}: {title}" if identifier else title
        return f"{header}\n\n{description}".strip()

    # --- outbound -----------------------------------------------------------

    def _token(self, target: ReplyTarget) -> str | None:
        """This workspace's own token, or nothing.

        A named workspace with no stored install fails closed. Falling back to
        the server's own credential there would post one tenant's run into
        whatever workspace that credential happens to belong to, which is the
        worst bug this adapter could have. The env fallback exists only for the
        single-tenant deployment, where no workspace id is in play at all.
        """
        workspace_id = target.tenant.tenant_id
        store = getattr(self.service, "linear_install_store", None)
        if workspace_id:
            return store.get_token(workspace_id) if store is not None else None
        return os.environ.get("FABRI_CRED_LINEAR_DEFAULT") or os.environ.get(
            "LINEAR_API_KEY"
        )

    def _comment(self, target: ReplyTarget, body: str) -> bool:
        token = self._token(target)
        issue_id = target.locator.get("issue_id")
        if not token or not issue_id:
            return False
        try:
            from fabri.integrations.linear import comment_issue

            comment_issue(issue_id, body, token=token)
            return True
        except Exception:
            _LOG.exception("could not comment on the Linear issue")
            return False

    def deliver_ack(self, target: ReplyTarget, text: str) -> bool:
        return self._comment(target, text)

    def deliver_result(self, target: ReplyTarget, outcome: RunOutcome) -> bool:
        lines = ["```", outcome.final_text, "```"]
        if outcome.total_cost_usd is not None:
            lines.append(f"_Cost: ${outcome.total_cost_usd:.4f}_")
        return self._comment(target, "\n".join(lines))

    def deliver_error(self, target: ReplyTarget, text: str) -> bool:
        return self._comment(target, text)
