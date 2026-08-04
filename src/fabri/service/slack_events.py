"""Slack Events API handling for @mention-driven Fabri runs.

The behaviour now lives in :mod:`fabri.service.surfaces.slack` (the adapter) and
:mod:`fabri.service.surfaces.pipeline` (dispatch policy shared by every
surface). This module stays as the Slack-shaped entry point: the HTTP server,
the ask_user bridge, and existing embedders keep calling these names.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fabri.service.surfaces import pipeline
from fabri.service.surfaces.slack import SlackAdapter, verify_slack_signature
from fabri.service.surfaces.types import ReplyTarget, TenantRef

__all__ = [
    "handle_message_event",
    "handle_slack_event",
    "register_thread",
    "route_question_to_thread",
    "verify_slack_signature",
]

_LOG = logging.getLogger("fabri")


def _target(channel: str, thread_ts: str, team_id: str | None = None) -> ReplyTarget:
    return ReplyTarget(
        TenantRef(surface="slack", tenant_id=team_id),
        {"channel": channel, "thread_ts": thread_ts},
    )


def register_thread(session_id: str, channel: str, thread_ts: str) -> None:
    """Associate a Fabri session with the Slack thread used for its questions."""
    pipeline.register_target(session_id, "slack", _target(channel, thread_ts))


def route_question_to_thread(
    slack_cfg: dict,
    session_id: str,
    task: str,
    question_id: str,
    question: str,
    options: list | None = None,
    default: str | None = None,
) -> bool:
    """Post a pending ask_user question in its run's Slack thread when possible."""
    adapter = SlackAdapter(slack_cfg)
    known = pipeline.target_for_session(session_id)
    target = known[1] if known and known[0] == "slack" else None
    if target is None:
        target = adapter.open_fallback_target(task)
        if target is None:
            return False
        pipeline.register_target(session_id, "slack", target)

    posted = adapter.deliver_question(
        target,
        {
            "question": question,
            "options": options,
            "default": default,
        },
    )
    if not posted:
        return False
    pipeline.set_pending_question(
        session_id, {"question_id": question_id, "options": options}
    )
    return True


def handle_message_event(
    event: dict, service: Any, slack_cfg: dict | None = None
) -> None:
    """Resolve a pending ask_user question from a human Slack thread reply."""
    adapter = SlackAdapter(slack_cfg or {}, service)
    decision = adapter._classify_message(
        event, TenantRef(surface="slack", tenant_id=event.get("team"))
    )
    target = getattr(decision, "target", None)
    text = getattr(decision, "text", None)
    if target is None or text is None:
        return
    if pipeline.deliver_answer(service, target, text):
        adapter.deliver_ack(target, "Got it.")


def handle_slack_event(
    raw_body: bytes,
    headers: Mapping[str, str],
    service: Any,
    slack_cfg: dict,
) -> tuple[int, str, dict[str, str]]:
    """Validate and acknowledge a Slack event, dispatching mentions asynchronously."""
    return pipeline.dispatch(
        SlackAdapter(slack_cfg, service), service, raw_body, headers
    )
