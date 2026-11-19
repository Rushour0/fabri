"""Jira Cloud as a surface adapter.

Add the label ``fabri:<ref>`` to an issue and that catalog entry runs against
the issue. The result comes back as a comment on the same issue. As with
Linear, a label is the trigger rather than a comment command: it is explicit,
visible on the issue, and cannot be typed by accident in a discussion.

A run starts when an issue is created with the label, or when an update adds
it (the changelog shows it in ``toString`` and not in ``fromString``). Other
edits to an already labelled issue start nothing, so editing the summary does
not rerun the entry. A label that names no catalog entry is ignored outright;
the pipeline's usual reply listing the catalog would itself be a write to the
issue.

Inbound security:

* Signature. Jira Cloud admin webhooks can be given a secret. This adapter
  expects ``X-Hub-Signature: sha256=<hex>``, an HMAC-SHA256 of the raw body
  keyed with ``JIRA_WEBHOOK_SECRET``, and refuses anything else. That header
  scheme follows Atlassian's description of webhook secrets but has NOT been
  checked against a live Jira site. Confirm it against a real delivery before
  relying on this surface; until then automations triggers pickup by polling.
* Replay. The body carries ``timestamp`` (milliseconds). A delivery older than
  five minutes is refused. Jira retries failed deliveries with the same
  ``X-Atlassian-Webhook-Identifier``, which the pipeline dedupes on.
* Tenant. Results go to the one site configured in ``JIRA_URL``. When that is
  a site URL, a delivery whose issue lives on another site is ignored, so one
  site's run can never be posted into another.

Outbound: every comment is sent through the Jira connector's own
``<name>_add_comment`` tool, built from ``connector_catalog.jira.expand`` and
run through a :class:`~fabri.tools.registry.ToolRegistry`. That tool is
``always_approve`` and floored by host, so the registry calls the approver
before anything is sent. With no approver the comment is refused and logged,
never sent. Credentials are the connector's: ``JIRA_EMAIL`` and
``JIRA_API_TOKEN`` for a site URL, or the stored OAuth connection named by
``JIRA_WORKSPACE`` for ``https://api.atlassian.com/ex/jira/<cloudId>``.
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
from urllib.parse import urlsplit

import httpx

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
from fabri.tools.registry import Approver

_LOG = logging.getLogger("fabri")

#: ``fabri:release-notes`` on an issue runs the release-notes entry against it.
LABEL_PREFIX = "fabri:"
SIGNATURE_HEADER = "X-Hub-Signature"
DELIVERY_HEADER = "X-Atlassian-Webhook-Identifier"
SOURCE_NAME = "jira"
_REPLAY_WINDOW_MS = 300_000
_TRIGGER_EVENTS = ("jira:issue_created", "jira:issue_updated")
# The approval card shows the full arguments and refuses anything longer than
# its cap (20000 characters), so the result text is cut well below that.
_MAX_COMMENT_CHARS = 15_000


def verify_jira_signature(secret: str, raw_body: bytes, header: str) -> bool:
    """``sha256=<hex>`` HMAC-SHA256 of the raw body, compared in constant time."""
    if not secret or not isinstance(header, str):
        return False
    scheme, _, digest = header.strip().partition("=")
    if scheme.lower() != "sha256" or not digest:
        return False
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, digest.strip().lower())


def _labels_from_string(value: object) -> set[str]:
    return set(value.split()) if isinstance(value, str) else set()


def _adf_text(node: object) -> str:
    """Plain text of an Atlassian Document Format node, paragraphs on new lines."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(child) for child in node)
    if not isinstance(node, Mapping):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    if node.get("type") == "hardBreak":
        return "\n"
    inner = _adf_text(node.get("content") or [])
    if node.get("type") in ("paragraph", "heading", "codeBlock", "listItem", "blockquote"):
        return inner + "\n"
    return inner


def _adf_document(text: str, footer: str | None = None) -> dict:
    content: list[dict] = [
        {"type": "codeBlock", "content": [{"type": "text", "text": text or "(empty)"}]}
    ]
    if footer:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": footer}]})
    return {"type": "doc", "version": 1, "content": content}


def _clip(text: str) -> str:
    if len(text) <= _MAX_COMMENT_CHARS:
        return text
    return text[:_MAX_COMMENT_CHARS] + "\n[cut: the full result is in the fabri run]"


class JiraAdapter(SurfaceAdapter):
    name = "jira"
    webhook_path = "/jira/webhook"

    def __init__(
        self,
        service: Any = None,
        *,
        approver: Approver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.service = service
        self.approver = approver
        self._transport = transport

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(hitl=False, threads=True)

    def required_env(self) -> list[str]:
        return ["JIRA_WEBHOOK_SECRET"]

    def outbound_env(self) -> list[list[str]]:
        return [
            ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
            ["JIRA_URL", "JIRA_WORKSPACE"],
            ["JIRA_URL", "JIRA_ACCESS_TOKEN"],
        ]

    # --- inbound ------------------------------------------------------------

    def verify(self, raw_body: bytes, headers: Mapping[str, str]):
        secret = os.environ.get("JIRA_WEBHOOK_SECRET", "")
        signature = headers.get(SIGNATURE_HEADER, "")
        if not secret or not verify_jira_signature(secret, raw_body, signature):
            return ShortCircuit(401, "invalid signature")
        try:
            payload = json.loads(raw_body or b"{}")
        except (TypeError, ValueError):
            return ShortCircuit(400, "invalid request JSON")
        if not isinstance(payload, dict):
            return ShortCircuit(400, "invalid request JSON")
        sent_at = payload.get("timestamp")
        now_ms = time.time() * 1000
        if (isinstance(sent_at, (int, float)) and not isinstance(sent_at, bool)
                and abs(now_ms - float(sent_at)) > _REPLAY_WINDOW_MS):
            return ShortCircuit(401, "stale delivery")
        # The delivery id lives in a header and delivery_id() only sees the
        # payload; carry it across the way the GitHub adapter does.
        payload["_delivery"] = headers.get(DELIVERY_HEADER, "") or ""
        return payload

    def delivery_id(self, payload: dict) -> str | None:
        delivery = payload.get("_delivery")
        return delivery if isinstance(delivery, str) and delivery else None

    def classify(self, payload: dict):
        event = payload.get("webhookEvent")
        if event not in _TRIGGER_EVENTS:
            return Ignore()
        issue = payload.get("issue")
        if not isinstance(issue, Mapping):
            return Ignore()
        key = issue.get("key")
        if not isinstance(key, str) or not key:
            return Ignore()
        raw_fields = issue.get("fields")
        fields: Mapping = raw_fields if isinstance(raw_fields, Mapping) else {}

        added = self._added_labels(event, fields, payload.get("changelog"))
        ref = self._ref_from_labels(added)
        if ref is None:
            return Ignore()
        catalog = getattr(self.service, "catalog", None) or {}
        if ref not in catalog:
            _LOG.info("jira label names no catalog entry; ignored", extra={"ref": ref})
            return Ignore()

        site = self._site_of(issue)
        if not self._site_allowed(site):
            _LOG.warning("jira delivery for a site other than JIRA_URL; ignored")
            return Ignore()

        target = ReplyTarget(
            TenantRef(self.name, site),
            {"issue_key": key, "issue_id": str(issue.get("id") or "")},
        )
        return Dispatch(Command(verb="run", task=self._task(key, fields), catalog_ref=ref), target)

    @staticmethod
    def _added_labels(event: str, fields: Mapping, changelog: object) -> list[str]:
        """Labels this delivery added. On create that is every label."""
        if event == "jira:issue_created":
            labels = fields.get("labels") or []
            return [label for label in labels if isinstance(label, str)]
        added: list[str] = []
        items = changelog.get("items") if isinstance(changelog, Mapping) else None
        for item in items or []:
            if not isinstance(item, Mapping) or item.get("field") != "labels":
                continue
            before = _labels_from_string(item.get("fromString"))
            after = item.get("toString")
            if isinstance(after, str):
                added.extend(label for label in after.split() if label not in before)
        return added

    @staticmethod
    def _ref_from_labels(labels: list[str]) -> str | None:
        for label in labels:
            if label.startswith(LABEL_PREFIX):
                ref = label[len(LABEL_PREFIX) :].strip()
                if ref:
                    return ref
        return None

    @staticmethod
    def _site_of(issue: Mapping) -> str | None:
        try:
            host = urlsplit(str(issue.get("self") or "")).hostname
        except ValueError:
            return None
        return host.lower() if host else None

    @staticmethod
    def _site_allowed(site: str | None) -> bool:
        """A site-URL config only answers its own site. The OAuth base names a
        cloud id, which a webhook body does not carry, so it cannot be checked
        here; the stored connection itself reaches exactly one site."""
        configured = os.environ.get("JIRA_URL", "")
        try:
            host = (urlsplit(configured).hostname or "").lower()
        except ValueError:
            return False
        if not host or host == "api.atlassian.com":
            return True
        return site == host

    @staticmethod
    def _task(key: str, fields: Mapping) -> str:
        """The issue is the task: key and summary, and the description for context."""
        summary = str(fields.get("summary") or "").strip()
        description = _adf_text(fields.get("description")).strip()
        return f"{key}: {summary}\n\n{description}".strip()

    # --- outbound -----------------------------------------------------------

    def _comment(self, target: ReplyTarget, body: dict) -> bool:
        """Post through the connector's always_approve comment tool.

        The registry asks the approver first and sends nothing without a yes.
        """
        key = target.locator.get("issue_key")
        url = os.environ.get("JIRA_URL", "")
        if not key or not url:
            return False
        try:
            from fabri.tools.connector_catalog import connector_entry
            from fabri.tools.registry import ToolRegistry
            from fabri.tools.sources import register_tool_sources

            expanded = connector_entry(
                "jira",
                name=SOURCE_NAME,
                url=url,
                workspace=os.environ.get("JIRA_WORKSPACE") or None,
            )
            tool_name = f"{SOURCE_NAME}_add_comment"
            source = next(s for s in expanded["sources"] if s["name"] == tool_name)
            registry = ToolRegistry([], approver=self.approver)
            errors = register_tool_sources(registry, [source], transport=self._transport)
            if errors:
                _LOG.warning("could not build the Jira comment tool: %s", "; ".join(errors))
                return False
            result = registry.invoke(tool_name, {"key": key, "body": body})
        except Exception:  # noqa: BLE001 -- a failed delivery is logged, never raised into the run thread
            _LOG.exception("could not comment on the Jira issue")
            return False
        if not result.get("ok"):
            _LOG.warning("Jira comment not sent: %s", result.get("error"))
            return False
        return True

    def deliver_result(self, target: ReplyTarget, outcome: RunOutcome) -> bool:
        footer = None
        if outcome.total_cost_usd is not None:
            footer = f"Cost: ${outcome.total_cost_usd:.4f}"
        return self._comment(target, _adf_document(_clip(outcome.final_text), footer))

    def deliver_error(self, target: ReplyTarget, text: str) -> bool:
        return self._comment(target, _adf_document(_clip(text)))
