"""Slack as a surface adapter.

The behaviour here is the behaviour ``slack_events`` had: v0 signature with a
replay window, the ``url_verification`` handshake answered before
authentication, per-team bot tokens overlaid from the install store, mentions
dispatched asynchronously, questions and answers round-tripping in the run's
own thread. What changed is where it lives — dispatch policy moved to the
pipeline, so Slack now only knows how to talk to Slack.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

from fabri.service.slack_notify import open_slack_thread, post_slack_message
from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.pipeline import parse_command
from fabri.service.surfaces.types import (
    Command,
    Dispatch,
    Handled,
    HitlAnswer,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    SurfaceCapabilities,
    TenantRef,
)

# Strip a leading Slack user/bot mention: <@U123>, <@U_BOT>, or <@U123|name>.
_MENTION_RE = re.compile(r"^\s*<@[^>]+>\s*")
_REPLAY_WINDOW_S = 300


def verify_slack_signature(
    signing_secret: str, timestamp: str, raw_body: bytes, signature: str
) -> bool:
    """Validate a Slack v0 signature and reject requests outside the replay window."""
    try:
        if abs(time.time() - int(timestamp)) > _REPLAY_WINDOW_S:
            return False
    except (TypeError, ValueError):
        return False
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


class SlackAdapter(SurfaceAdapter):
    name = "slack"
    webhook_path = "/slack/events"

    def __init__(self, slack_cfg: Mapping | None = None, service: Any = None) -> None:
        self.slack_cfg = dict(slack_cfg or {})
        self.service = service

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(hitl=True, threads=True)

    # --- inbound ------------------------------------------------------------

    def verify(self, raw_body: bytes, headers: Mapping[str, str]):
        try:
            payload = json.loads(raw_body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ShortCircuit(400, "invalid request JSON")
        if not isinstance(payload, dict):
            return ShortCircuit(400, "invalid request JSON")
        # The URL handshake is answered before authentication, by Slack's own
        # design: the endpoint has no shared secret to check against yet.
        if payload.get("type") == "url_verification":
            return ShortCircuit(
                200,
                str(payload.get("challenge", "")),
                {"Content-Type": "text/plain"},
            )
        secret_env = self.slack_cfg.get("signing_secret_env", "SLACK_SIGNING_SECRET")
        signing_secret = os.environ.get(secret_env)
        if not signing_secret or not verify_slack_signature(
            signing_secret,
            headers.get("X-Slack-Request-Timestamp", ""),
            raw_body,
            headers.get("X-Slack-Signature", ""),
        ):
            return ShortCircuit(401, "invalid signature")
        return payload

    def delivery_id(self, payload: dict) -> str | None:
        event_id = payload.get("event_id")
        return event_id if isinstance(event_id, str) and event_id else None

    def classify(self, payload: dict):
        team_id = payload.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            team_id = None
        event = payload.get("event")
        if not isinstance(event, dict):
            return Ignore()

        etype = event.get("type")
        if etype in ("app_uninstalled", "tokens_revoked"):
            if team_id and self.service is not None:
                self.service.install_store.delete(team_id)
            return Handled()

        tenant = TenantRef(surface=self.name, tenant_id=team_id)

        if etype == "message":
            return self._classify_message(event, tenant)
        if etype != "app_mention":
            return Ignore()

        channel = event.get("channel")
        event_ts = event.get("ts")
        if not isinstance(channel, str) or not isinstance(event_ts, str):
            return Ignore()
        thread_ts = event.get("thread_ts") or event_ts
        raw_text = event.get("text", "")
        text = _MENTION_RE.sub("", raw_text, count=1).strip() if isinstance(raw_text, str) else ""
        target = ReplyTarget(tenant, {"channel": channel, "thread_ts": thread_ts})

        command = parse_command(text)
        if command is None:
            # No verb: the pre-grammar behaviour, where the whole message is the
            # task for a server-configured agency. Kept so existing single-
            # agency deployments do not break on upgrade.
            command = Command(
                verb="run", task=text, catalog_ref=self.slack_cfg.get("mention_agency")
            )
        return Dispatch(command, target)

    def _classify_message(self, event: dict, tenant: TenantRef):
        """A thread reply, which may be answering a question the run asked."""
        if (
            event.get("bot_id")
            or event.get("subtype")
            or not isinstance(event.get("thread_ts"), str)
        ):
            return Ignore()
        channel = event.get("channel")
        thread_ts = event.get("thread_ts")
        text = event.get("text")
        if (
            not isinstance(channel, str)
            or not isinstance(thread_ts, str)
            or not isinstance(text, str)
        ):
            return Ignore()
        return HitlAnswer(
            ReplyTarget(tenant, {"channel": channel, "thread_ts": thread_ts}), text
        )

    # --- outbound -----------------------------------------------------------

    def _cfg_for(self, target: ReplyTarget) -> dict:
        """This tenant's Slack config: its own bot token when we have one.

        Falls back to the server's env-configured token, which is the
        single-tenant path that predates the install store.
        """
        team_id = target.tenant.tenant_id
        store = getattr(self.service, "install_store", None)
        token = store.get_token(team_id) if (store is not None and team_id) else None
        if token:
            return {**self.slack_cfg, "bot_token": token, "enabled": True}
        return self.slack_cfg

    def _post(self, target: ReplyTarget, text: str) -> bool:
        channel = target.locator.get("channel")
        thread_ts = target.locator.get("thread_ts")
        if not channel:
            return False
        return bool(post_slack_message(self._cfg_for(target), text, channel, thread_ts))

    def deliver_ack(self, target: ReplyTarget, text: str) -> bool:
        return self._post(target, text)

    def deliver_result(self, target: ReplyTarget, outcome: RunOutcome) -> bool:
        lines = [outcome.final_text]
        if outcome.total_cost_usd is not None:
            lines.append(f"_Cost: ${outcome.total_cost_usd:.4f}_")
        studio = self.slack_cfg.get("studio_url")
        if studio:
            lines.append(f"{str(studio).rstrip('/')}/#replay/{outcome.session_id}")
        return self._post(target, "\n".join(lines))

    def deliver_error(self, target: ReplyTarget, text: str) -> bool:
        return self._post(target, text)

    def deliver_question(self, target: ReplyTarget, question: dict) -> bool:
        lines = [f"Question: {question.get('question', '')}", "Please reply in this thread."]
        options = question.get("options")
        if options:
            lines.append("Reply with one of: " + " / ".join(str(o) for o in options))
        if question.get("default") is not None:
            lines.append(f"Default: {question['default']}")
        return self._post(target, "\n".join(lines))

    def open_fallback_target(self, task: str) -> ReplyTarget | None:
        """A thread to ask in when the run did not start on Slack.

        Studio-started runs have no Slack thread, so a question would have
        nowhere to go; a server with ``owned_channel`` set opens one.
        """
        owned_channel = self.slack_cfg.get("owned_channel")
        if not isinstance(owned_channel, str) or not owned_channel:
            return None
        thread_ts = open_slack_thread(self.slack_cfg, owned_channel, f"New run: {task}")
        if thread_ts is None:
            return None
        return ReplyTarget(
            TenantRef(surface=self.name),
            {"channel": owned_channel, "thread_ts": thread_ts},
        )
