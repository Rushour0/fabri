"""Slack Events API handling for @mention-driven Fabri runs."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from threading import Lock, Thread
from typing import Any

from fabri.service.slack_notify import post_slack_message

# Strip a leading Slack user/bot mention: <@U123>, <@U_BOT>, or <@U123|name>.
_MENTION_RE = re.compile(r"^\s*<@[^>]+>\s*")
_MAX_EVENT_IDS = 512
_event_ids: OrderedDict[str, None] = OrderedDict()
_event_ids_lock = Lock()


def verify_slack_signature(
    signing_secret: str, timestamp: str, raw_body: bytes, signature: str
) -> bool:
    """Validate a Slack v0 signature and reject requests outside the replay window."""
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def _is_duplicate(event_id: object) -> bool:
    if not isinstance(event_id, str) or not event_id:
        return False
    with _event_ids_lock:
        if event_id in _event_ids:
            return True
        _event_ids[event_id] = None
        if len(_event_ids) > _MAX_EVENT_IDS:
            _event_ids.popitem(last=False)
    return False


def _run_mention(
    service: Any, slack_cfg: dict, task: str, channel: str, thread_ts: str
) -> None:
    post_slack_message(slack_cfg, "On it...", channel, thread_ts)
    try:
        catalog_ref = slack_cfg.get("mention_agency")
        session_id = service.submit(task, catalog_ref=catalog_ref)
        result = service.result(session_id)
        final_text = result.get("final_text") or "The run finished without a final response."
        post_slack_message(slack_cfg, str(final_text), channel, thread_ts)
    except Exception:
        post_slack_message(
            slack_cfg,
            "I couldn't complete that run. Check the Fabri server logs for details.",
            channel,
            thread_ts,
        )


def handle_slack_event(
    raw_body: bytes,
    headers: Mapping[str, str],
    service: Any,
    slack_cfg: dict,
) -> tuple[int, str, dict[str, str]]:
    """Validate and acknowledge a Slack event, dispatching mentions asynchronously."""
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 400, "invalid request JSON", {}
    if not isinstance(payload, dict):
        return 400, "invalid request JSON", {}
    if payload.get("type") == "url_verification":
        return 200, str(payload.get("challenge", "")), {"Content-Type": "text/plain"}

    secret_env = slack_cfg.get("signing_secret_env", "SLACK_SIGNING_SECRET")
    signing_secret = os.environ.get(secret_env)
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    signature = headers.get("X-Slack-Signature", "")
    if not signing_secret or not verify_slack_signature(
        signing_secret, timestamp, raw_body, signature
    ):
        return 401, "invalid signature", {}
    if _is_duplicate(payload.get("event_id")):
        return 200, "", {}

    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "app_mention":
        return 200, "", {}
    channel = event.get("channel")
    event_ts = event.get("ts")
    if not isinstance(channel, str) or not isinstance(event_ts, str):
        return 200, "", {}
    thread_ts = event.get("thread_ts") or event_ts
    text = event.get("text", "")
    task = _MENTION_RE.sub("", text, count=1).strip() if isinstance(text, str) else ""
    Thread(
        target=_run_mention,
        args=(service, slack_cfg, task, channel, thread_ts),
        daemon=True,
    ).start()
    return 200, "", {}
