"""Best-effort outbound Slack notifications for human-in-the-loop questions."""
from __future__ import annotations

import json
import logging
import os
import urllib.request


_LOG = logging.getLogger("fabri")
_CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def post_slack_message(
    slack_cfg: dict,
    text: str,
    channel: str,
    thread_ts: str | None = None,
) -> bool:
    """Best-effort post of one message to a Slack channel or thread."""
    if not slack_cfg.get("enabled"):
        return False

    token_env = slack_cfg.get("bot_token_env", "SLACK_BOT_TOKEN")
    token = os.environ.get(token_env)
    if not token:
        _LOG.warning("Slack notification skipped: token environment variable %s is unset", token_env)
        return False

    payload: dict[str, str] = {"channel": channel, "text": text}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    try:
        request = urllib.request.Request(
            _CHAT_POST_MESSAGE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception:
        _LOG.warning("Slack message post failed", exc_info=True)
        return False

    if not isinstance(result, dict) or not result.get("ok"):
        _LOG.warning(
            "Slack message was rejected: %s",
            result.get("error", "unknown error") if isinstance(result, dict) else "invalid response",
        )
        return False
    return True


def post_ask_user_question(
    slack_cfg: dict,
    *,
    session_id: str,
    question: str,
    question_id: str,
    options: list | None = None,
    default: str | None = None,
) -> bool:
    """Best-effort post of an ask_user question to Slack.

    Returns ``True`` only when Slack accepts the message. Notification failures
    are logged and never allowed to interrupt the running agent.
    """
    if not slack_cfg.get("enabled"):
        return False

    token_env = slack_cfg.get("bot_token_env", "SLACK_BOT_TOKEN")
    token = os.environ.get(token_env)
    if not token:
        _LOG.warning("Slack notification skipped: token environment variable %s is unset", token_env)
        return False

    target = slack_cfg.get("default_channel") or slack_cfg.get("default_user")
    if not target:
        return False

    text_lines = [
        "Fabri agent needs input.",
        f"Session ID: {session_id}",
        f"Question ID: {question_id}",
        f"Question: {question}",
    ]
    if options:
        text_lines.append("Options: " + ", ".join(str(option) for option in options))
    if default is not None:
        text_lines.append(f"Default: {default}")
    studio_base_url = slack_cfg.get("studio_base_url")
    if studio_base_url:
        text_lines.append(f"Answer in Studio: {studio_base_url}")

    try:
        payload = json.dumps({"channel": target, "text": "\n".join(text_lines)}).encode("utf-8")
        request = urllib.request.Request(
            _CHAT_POST_MESSAGE_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception:
        _LOG.warning("Slack notification failed for session %s", session_id, exc_info=True)
        return False

    if not isinstance(result, dict) or not result.get("ok"):
        _LOG.warning(
            "Slack notification was rejected for session %s: %s",
            session_id,
            result.get("error", "unknown error") if isinstance(result, dict) else "invalid response",
        )
        return False
    return True
