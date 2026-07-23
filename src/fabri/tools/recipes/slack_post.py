"""Post a message to a Slack channel, optionally as a threaded reply."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

# Recipe scripts are launched directly from their manifests. Keep imports
# working from a source checkout without requiring an editable installation.
if not __package__:
    for _candidate in Path(__file__).resolve().parents:
        if (_candidate / "fabri" / "tools" / "secret_refs.py").is_file():
            sys.path.insert(0, str(_candidate))
            break

from fabri.service.slack_notify import _CHAT_POST_MESSAGE_URL
from fabri.tools.secret_refs import resolve_secret

_REQUEST_TIMEOUT_S = 5


def _error(message: str, token: str | None = None) -> dict[str, Any]:
    """Build an error result while defensively redacting the resolved token."""
    if token:
        message = message.replace(token, "[REDACTED]")
    return {"ok": False, "error": message}


def post_message(args: dict[str, Any]) -> dict[str, Any]:
    """Resolve credentials, call ``chat.postMessage``, and normalize its result."""
    channel = args.get("channel")
    text = args.get("text")
    thread_ts = args.get("thread_ts")
    credential = args.get("credential", "slack:default")

    if not isinstance(channel, str) or not channel:
        return _error("channel must be a non-empty string")
    if not isinstance(text, str) or not text:
        return _error("text must be a non-empty string")
    if thread_ts is not None and (
        not isinstance(thread_ts, str) or not thread_ts
    ):
        return _error("thread_ts must be a non-empty string when provided")
    if not isinstance(credential, str):
        return _error("credential must be a Slack credential reference")

    try:
        token = resolve_secret(credential)
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")

    payload = {"channel": channel, "text": text}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    request = urllib.request.Request(
        _CHAT_POST_MESSAGE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
            raw_result = response.read().decode("utf-8")
        result = json.loads(raw_result)
    except Exception as exc:
        # Exception messages can include request details. Return only the type
        # so an Authorization header can never be copied into tool output.
        return _error(f"Slack request failed ({type(exc).__name__})", token)

    if not isinstance(result, dict):
        return _error("Slack API returned an invalid response", token)
    if not result.get("ok"):
        slack_error = result.get("error", "unknown_error")
        if not isinstance(slack_error, str) or not slack_error:
            slack_error = "unknown_error"
        return _error(slack_error, token)

    ts = result.get("ts")
    response_channel = result.get("channel")
    if not isinstance(ts, str) or not isinstance(response_channel, str):
        return _error("Slack API response is missing ts or channel", token)
    return {"ok": True, "result": {"ts": ts, "channel": response_channel}}


def main() -> int:
    try:
        args = json.loads(sys.stdin.read())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps(_error(f"invalid input JSON ({type(exc).__name__})")))
        return 1
    if not isinstance(args, dict):
        print(json.dumps(_error("input must be a JSON object")))
        return 1

    result = post_message(args)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
