"""Slack as a surface adapter.

The behaviour here is the behaviour ``slack_events`` had: v0 signature with a
replay window, the ``url_verification`` handshake answered before
authentication, per-team bot tokens overlaid from the install store, mentions
dispatched asynchronously, questions and answers round-tripping in the run's
own thread. What changed is where it lives — dispatch policy moved to the
pipeline, so Slack now only knows how to talk to Slack.

Two seams let a host run the receiver stateless: a :class:`SlackPoster` for
every outbound call and a :class:`SlackSecrets` for the signing secret and bot
tokens. Both default to what the adapter did before they existed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from fabri.service.slack_notify import (
    open_slack_thread,
    post_slack_message,
    update_slack_message,
)
from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.pipeline import parse_command
from fabri.service.surfaces.types import (
    Command,
    Dispatch,
    Feedback,
    Handled,
    HitlAnswer,
    Ignore,
    Interaction,
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


def _header(headers: Any, name: str) -> str:
    """One request header, whatever holds them.

    ``http.client.HTTPMessage`` already matches names case-insensitively; a
    plain dict does not, so fall back to a scan.
    """
    if headers is None:
        return ""
    value = headers.get(name)
    if value is None and hasattr(headers, "items"):
        lowered = name.lower()
        value = next(
            (v for k, v in headers.items() if str(k).lower() == lowered), None
        )
    return value if isinstance(value, str) else ""


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# --- seams: how the adapter posts, and where its secrets come from ------------


@runtime_checkable
class SlackPoster(Protocol):
    """Outbound Slack calls.

    ``token_cfg`` is the slack config the default hands to
    :mod:`fabri.service.slack_notify`: ``bot_token`` when a token was resolved,
    and ``enabled``.
    """

    def post(
        self,
        token_cfg: dict,
        channel: str,
        text: str,
        thread_ts: str | None,
        blocks: list | None,
    ) -> bool: ...

    def update(
        self,
        token_cfg: dict,
        channel: str,
        ts: str,
        text: str,
        blocks: list | None,
    ) -> bool: ...


class DefaultSlackPoster:
    """Posts through :mod:`fabri.service.slack_notify`, as the adapter always has."""

    def post(
        self,
        token_cfg: dict,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list | None = None,
    ) -> bool:
        if blocks is None:
            return bool(post_slack_message(token_cfg, text, channel, thread_ts))
        return bool(
            post_slack_message(token_cfg, text, channel, thread_ts, blocks=blocks)
        )

    def update(
        self,
        token_cfg: dict,
        channel: str,
        ts: str,
        text: str,
        blocks: list | None = None,
    ) -> bool:
        return bool(update_slack_message(token_cfg, channel, ts, text, blocks))


@runtime_checkable
class SlackSecrets(Protocol):
    """Where the signing secret and bot tokens come from.

    A host with a secret manager injects one, and the receiver then needs no
    Slack env vars at all.
    """

    def signing_secret(self) -> str | None: ...

    def bot_token(self, team_id: str | None) -> str | None: ...


class DefaultSlackSecrets:
    """Env vars and the install store, exactly as before the seam existed."""

    def __init__(self, slack_cfg: Mapping, service: Any = None) -> None:
        self.slack_cfg = slack_cfg
        self.service = service

    def signing_secret(self) -> str | None:
        secret_env = self.slack_cfg.get("signing_secret_env", "SLACK_SIGNING_SECRET")
        return os.environ.get(secret_env)

    def install_token(self, team_id: str | None) -> str | None:
        """The token this team installed the app with, if we hold one."""
        store = getattr(self.service, "install_store", None)
        return store.get_token(team_id) if (store is not None and team_id) else None

    def bot_token(self, team_id: str | None) -> str | None:
        token = self.install_token(team_id)
        if token:
            return token
        token_env = self.slack_cfg.get("bot_token_env", "SLACK_BOT_TOKEN")
        return self.slack_cfg.get("bot_token") or os.environ.get(token_env)


class SlackAdapter(SurfaceAdapter):
    """Slack events, and optionally Slack interactivity (button clicks).

    Settings read from ``slack_cfg`` besides the credentials:

    - ``channel_allowlist``: channel ids to act on. Absent means every channel;
      present and empty means none. Applies to mentions, watched-channel posts,
      feedback and button clicks. A reply to a pending question is not
      filtered: it only ever matches a thread a run already registered.
    - ``watched_channels``: ``{channel_id: catalog_ref}``. A top-level human
      post there starts a run of that ref; a human reply in one of its threads
      becomes :class:`Feedback`.
    - ``bot_user_id``: our own bot's user id, so its posts never trigger runs.
      ``authorizations[*].user_id`` with ``is_bot`` in the payload counts too.
    - ``interactivity_enabled``: also answer on ``/slack/interactivity``.
    """

    name = "slack"
    webhook_path = "/slack/events"
    interactivity_path = "/slack/interactivity"

    def __init__(
        self,
        slack_cfg: Mapping | None = None,
        service: Any = None,
        *,
        poster: SlackPoster | None = None,
        secrets: SlackSecrets | None = None,
    ) -> None:
        self.slack_cfg = dict(slack_cfg or {})
        self.service = service
        self.poster: SlackPoster = poster or DefaultSlackPoster()
        self._secrets_injected = secrets is not None
        self.secrets: SlackSecrets = secrets or DefaultSlackSecrets(
            self.slack_cfg, service
        )

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(hitl=True, threads=True)

    def webhook_paths(self) -> tuple[str, ...]:
        if self.slack_cfg.get("interactivity_enabled"):
            return (self.webhook_path, self.interactivity_path)
        return (self.webhook_path,)

    def required_env(self) -> list[str]:
        if self._secrets_injected:
            return []
        return [self.slack_cfg.get("signing_secret_env", "SLACK_SIGNING_SECRET")]

    def outbound_env(self) -> list[list[str]]:
        if self._secrets_injected or self.slack_cfg.get("bot_token"):
            return []
        return [[self.slack_cfg.get("bot_token_env", "SLACK_BOT_TOKEN")]]

    def install_count(self) -> int | None:
        store = getattr(self.service, "install_store", None)
        return len(store.list()) if store is not None else None

    # --- inbound ------------------------------------------------------------

    def _signature_ok(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        signing_secret = self.secrets.signing_secret()
        return bool(signing_secret) and verify_slack_signature(
            signing_secret,
            _header(headers, "X-Slack-Request-Timestamp"),
            raw_body,
            _header(headers, "X-Slack-Signature"),
        )

    @staticmethod
    def _is_form(raw_body: bytes, headers: Mapping[str, str]) -> bool:
        content_type = _header(headers, "Content-Type").lower()
        if content_type.startswith("application/x-www-form-urlencoded"):
            return True
        return isinstance(raw_body, (bytes, bytearray)) and raw_body.startswith(
            b"payload="
        )

    def _verify_interaction(self, raw_body: bytes, headers: Mapping[str, str]):
        """A button click: a form body with one ``payload`` field of JSON.

        The signature covers the raw form body, so it is checked before any
        parsing, and a failure stops there.
        """
        if not self._signature_ok(raw_body, headers):
            return ShortCircuit(401, "invalid signature")
        try:
            fields = urllib.parse.parse_qs(raw_body.decode("utf-8"), strict_parsing=True)
            payload = json.loads(fields["payload"][0])
        except (KeyError, IndexError, UnicodeDecodeError, ValueError):
            return ShortCircuit(400, "invalid interaction payload")
        if not isinstance(payload, dict):
            return ShortCircuit(400, "invalid interaction payload")
        return payload

    def verify(self, raw_body: bytes, headers: Mapping[str, str]):
        if self._is_form(raw_body, headers):
            return self._verify_interaction(raw_body, headers)
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
        if not self._signature_ok(raw_body, headers):
            return ShortCircuit(401, "invalid signature")
        return payload

    def delivery_id(self, payload: dict) -> str | None:
        if payload.get("type") == "block_actions":
            return _interaction_delivery_id(payload)
        event_id = payload.get("event_id")
        return event_id if isinstance(event_id, str) and event_id else None

    # --- policy helpers -----------------------------------------------------

    def _channel_allowed(self, channel: object) -> bool:
        if "channel_allowlist" not in self.slack_cfg:
            return True
        allowlist = self.slack_cfg.get("channel_allowlist") or ()
        return isinstance(channel, str) and channel in allowlist

    def _watched_ref(self, channel: object) -> str | None:
        watched = self.slack_cfg.get("watched_channels")
        if not isinstance(watched, Mapping) or not isinstance(channel, str):
            return None
        ref = watched.get(channel)
        return ref if isinstance(ref, str) and ref else None

    def _is_own_bot(self, payload: dict, event: dict) -> bool:
        user = event.get("user")
        if not isinstance(user, str) or not user:
            return False
        if user == self.slack_cfg.get("bot_user_id"):
            return True
        authorizations = payload.get("authorizations")
        if not isinstance(authorizations, list):
            return False
        return any(
            isinstance(auth, dict) and auth.get("is_bot") and auth.get("user_id") == user
            for auth in authorizations
        )

    def classify(self, payload: dict):
        if payload.get("type") == "block_actions":
            return self._classify_interaction(payload)
        if payload.get("type") in _OTHER_INTERACTION_TYPES:
            return Ignore()
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

        if self._is_own_bot(payload, event):
            return Ignore()
        if etype == "message":
            return self._classify_message(event, tenant, payload)
        if etype != "app_mention":
            return Ignore()

        channel = event.get("channel")
        event_ts = event.get("ts")
        if not isinstance(channel, str) or not isinstance(event_ts, str):
            return Ignore()
        if not self._channel_allowed(channel):
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
        return Dispatch(command, target, _trigger_meta("mention", payload, event, channel))

    def _classify_message(
        self, event: dict, tenant: TenantRef, payload: dict | None = None
    ):
        """A thread reply, which may be answering a question the run asked, or
        a human post in a watched channel."""
        payload = payload if isinstance(payload, dict) else {}
        if event.get("bot_id") or event.get("subtype"):
            return Ignore()
        watched_ref = self._watched_ref(event.get("channel"))
        if watched_ref is not None:
            return self._classify_watched(event, tenant, payload, watched_ref)
        if not isinstance(event.get("thread_ts"), str):
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

    def _classify_watched(
        self, event: dict, tenant: TenantRef, payload: dict, catalog_ref: str
    ):
        """A new post in a watched channel starts a run of its ref; a reply in
        one of its threads is feedback on that run."""
        channel = event.get("channel")
        ts = event.get("ts")
        text = event.get("text")
        thread_ts = event.get("thread_ts")
        if (
            not isinstance(channel, str)
            or not isinstance(ts, str)
            or not isinstance(text, str)
            or (thread_ts is not None and not isinstance(thread_ts, str))
        ):
            return Ignore()
        if not self._channel_allowed(channel):
            return Ignore()
        if thread_ts is None or thread_ts == ts:
            target = ReplyTarget(tenant, {"channel": channel, "thread_ts": ts})
            command = Command(verb="run", task=text, catalog_ref=catalog_ref)
            return Dispatch(
                command, target, _trigger_meta("channel_post", payload, event, channel)
            )
        user = event.get("user")
        return Feedback(
            ReplyTarget(tenant, {"channel": channel, "thread_ts": thread_ts}),
            text,
            user=user if isinstance(user, str) else None,
            ts=ts,
            meta=_trigger_meta("thread_reply", payload, event, channel),
        )

    def _classify_interaction(self, payload: dict):
        """A ``block_actions`` click on a message in some thread."""
        container = _as_dict(payload.get("container"))
        message = _as_dict(payload.get("message"))
        channel = _as_dict(payload.get("channel")).get("id") or container.get("channel_id")
        message_ts = container.get("message_ts") or message.get("ts")
        thread_ts = container.get("thread_ts") or message.get("thread_ts") or message_ts
        if not isinstance(channel, str) or not channel or not isinstance(thread_ts, str):
            return Ignore()
        if not self._channel_allowed(channel):
            return Ignore()
        team_id = _as_dict(payload.get("team")).get("id")
        user = _as_dict(payload.get("user")).get("id")
        user = user if isinstance(user, str) and user else None
        actions = payload.get("actions")
        state_values = _as_dict(payload.get("state")).get("values")
        target = ReplyTarget(
            TenantRef(
                surface=self.name,
                tenant_id=team_id if isinstance(team_id, str) and team_id else None,
            ),
            {"channel": channel, "thread_ts": thread_ts},
        )
        meta = {"trigger": "interaction", "channel": channel}
        for key, value in (
            ("delivery_id", _interaction_delivery_id(payload)),
            ("trigger_id", payload.get("trigger_id")),
            ("response_url", payload.get("response_url")),
            ("user", user),
        ):
            if isinstance(value, str) and value:
                meta[key] = value
        return Interaction(
            target,
            user=user,
            actions=list(actions) if isinstance(actions, list) else [],
            state_values=dict(state_values) if isinstance(state_values, dict) else {},
            message_ts=message_ts if isinstance(message_ts, str) else None,
            meta=meta,
        )

    # --- outbound -----------------------------------------------------------

    def _cfg_for(self, target: ReplyTarget) -> dict:
        """This tenant's Slack config: its own bot token when we have one.

        Falls back to the server's env-configured token, which is the
        single-tenant path that predates the install store.
        """
        team_id = target.tenant.tenant_id
        if self._secrets_injected:
            # An injected resolver is the whole truth about tokens.
            token = self.secrets.bot_token(team_id)
        else:
            # The default overlays only an installed team's token here. The env
            # token stays behind the config's own ``enabled`` switch, as before.
            token = self.secrets.install_token(team_id)  # type: ignore[attr-defined]
        if token:
            return {**self.slack_cfg, "bot_token": token, "enabled": True}
        return self.slack_cfg

    def _post(self, target: ReplyTarget, text: str, blocks: list | None = None) -> bool:
        channel = target.locator.get("channel")
        thread_ts = target.locator.get("thread_ts")
        if not channel:
            return False
        return bool(
            self.poster.post(self._cfg_for(target), channel, text, thread_ts, blocks)
        )

    def update_message(
        self, target: ReplyTarget, ts: str, text: str, blocks: list | None = None
    ) -> bool:
        """Edit a message we posted, such as a card whose button was clicked."""
        channel = target.locator.get("channel")
        if not channel:
            return False
        return bool(self.poster.update(self._cfg_for(target), channel, ts, text, blocks))

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


# Interactive payload types other than button clicks. Signed and parsed, then
# ignored: nothing here acts on them yet.
_OTHER_INTERACTION_TYPES = frozenset(
    {
        "view_submission",
        "view_closed",
        "shortcut",
        "message_action",
        "interactive_message",
        "block_suggestion",
    }
)


def _trigger_meta(trigger: str, payload: dict, event: dict, channel: str) -> dict:
    meta = {"trigger": trigger, "channel": channel}
    for key, value in (
        ("event_id", payload.get("event_id")),
        ("user", event.get("user")),
        ("ts", event.get("ts")),
        ("thread_ts", event.get("thread_ts")),
    ):
        if isinstance(value, str) and value:
            meta[key] = value
    return meta


def _interaction_delivery_id(payload: dict) -> str | None:
    """``interaction:<trigger_id>``, or a stand-in built from the clicked
    message's ts, the first action's ts and the user id."""
    trigger_id = payload.get("trigger_id")
    if isinstance(trigger_id, str) and trigger_id:
        return "interaction:" + trigger_id
    actions = payload.get("actions")
    first = _as_dict(actions[0]) if isinstance(actions, list) and actions else {}
    parts = (
        _as_dict(payload.get("container")).get("message_ts"),
        first.get("action_ts"),
        _as_dict(payload.get("user")).get("id"),
    )
    if not all(isinstance(p, str) and p for p in parts):
        return None
    return "interaction:" + ":".join(parts)
