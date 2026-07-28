from __future__ import annotations

from typing import Any

from fabri.tools.recipes import slack_post


def notify_slack(
    text: str,
    *,
    channel: str,
    credential: str = "slack:default",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Post a repo-run status line to Slack via the slack_post recipe.

    Returns the recipe's {ok, result?, error?} dict verbatim; caller gates on result['ok'].
    """
    args: dict[str, Any] = {
        "channel": channel,
        "text": text,
        "credential": credential,
    }
    if thread_ts is not None:
        args["thread_ts"] = thread_ts
    return slack_post.post_message(args)
