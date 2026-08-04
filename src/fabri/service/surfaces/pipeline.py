"""The run pipeline every surface shares.

verify -> dedupe -> classify -> resolve the catalog ref -> submit -> deliver.

Written exactly once, on purpose. Everything here is policy that a new
integration must not be able to forget: replay protection, catalog validation,
recording where a run came from, and disabling ``ask_user`` on surfaces that
have no way to ask. An adapter supplies transport and wording; the pipeline
supplies behaviour.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Mapping
from threading import Lock, Thread
from typing import Any

from fabri.service.surfaces import quota
from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.types import (
    Command,
    Dispatch,
    Handled,
    HitlAnswer,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
)

_LOG = logging.getLogger("fabri")

_MAX_DELIVERY_IDS = 512
_delivery_ids: dict[str, OrderedDict[str, None]] = {}
_delivery_lock = Lock()

# session_id <-> where that run came from, so a mid-run question and a finished
# result both find their way home. Held here rather than in any one adapter:
# the ask_user bridge in FabriService must not know what Slack is.
_target_by_session: dict[str, tuple[str, ReplyTarget]] = {}
_session_by_target: dict[tuple, str] = {}
_pending_question: dict[str, dict] = {}
_target_lock = Lock()

_OK = (200, "", {})


# --- the grammar, shared so every surface reads the same ---------------------

_RUN_RE = re.compile(r"^run\s+(?P<ref>[\w.:/-]+)\s*(?P<task>.*)$", re.DOTALL | re.I)


def parse_command(text: str) -> Command | None:
    """``run <ref> <task>`` / ``list`` / ``help``, or None if it isn't a command.

    Deterministic on purpose: routing a chat message through a model would add
    a cost and a failure mode to every inbound message, and "did it understand
    me" is exactly the wrong first impression for an integration to make.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    lowered = cleaned.casefold()
    if lowered in ("list", "ls", "catalog"):
        return Command(verb="list")
    if lowered in ("help", "?"):
        return Command(verb="help")
    match = _RUN_RE.match(cleaned)
    if match:
        return Command(
            verb="run",
            catalog_ref=match.group("ref"),
            task=match.group("task").strip(),
        )
    return None


def render_catalog(service: Any, *, intro: str = "") -> str:
    """The refs a surface can actually run, as plain text."""
    catalog = getattr(service, "catalog", None) or {}
    if not catalog:
        return "This server has no catalog loaded, so there is nothing to run by name."
    lines = [intro] if intro else []
    lines.append("Run one with: `run <name> <what you want done>`")
    for name in sorted(catalog):
        entry = catalog.get(name) or {}
        meta = entry.get("meta") or {}
        title = meta.get("title") or meta.get("tagline") or entry.get("kind") or ""
        lines.append(f"• `{name}`" + (f" — {title}" if title else ""))
    return "\n".join(lines)


# --- HITL target registry ----------------------------------------------------


def register_target(session_id: str, surface: str, target: ReplyTarget) -> None:
    """Remember where a run came from, so questions and results go back there."""
    with _target_lock:
        previous = _target_by_session.get(session_id)
        if previous is not None:
            _session_by_target.pop(previous[1].key(), None)
        _target_by_session[session_id] = (surface, target)
        _session_by_target[target.key()] = session_id


def target_for_session(session_id: str) -> tuple[str, ReplyTarget] | None:
    with _target_lock:
        return _target_by_session.get(session_id)


def session_for_target(target: ReplyTarget) -> str | None:
    with _target_lock:
        return _session_by_target.get(target.key())


def set_pending_question(session_id: str, question: dict) -> None:
    with _target_lock:
        _pending_question[session_id] = question


def take_pending_question(session_id: str) -> dict | None:
    with _target_lock:
        return _pending_question.get(session_id)


def clear_pending_question(session_id: str) -> None:
    with _target_lock:
        _pending_question.pop(session_id, None)


def reset_state() -> None:
    """Drop all cross-run state. Tests only."""
    with _target_lock:
        _target_by_session.clear()
        _session_by_target.clear()
        _pending_question.clear()
    with _delivery_lock:
        _delivery_ids.clear()


def _is_duplicate(surface: str, delivery_id: object) -> bool:
    if not isinstance(delivery_id, str) or not delivery_id:
        return False
    with _delivery_lock:
        seen = _delivery_ids.setdefault(surface, OrderedDict())
        if delivery_id in seen:
            return True
        seen[delivery_id] = None
        if len(seen) > _MAX_DELIVERY_IDS:
            seen.popitem(last=False)
    return False


# --- answering a question from the surface ----------------------------------


def deliver_answer(service: Any, target: ReplyTarget, text: str) -> bool:
    """Feed a human's surface reply back into the waiting run."""
    session_id = session_for_target(target)
    if session_id is None:
        return False
    pending = take_pending_question(session_id)
    if not pending:
        return False
    options = pending.get("options") or []
    selected = next(
        (
            str(option)
            for option in options
            if text.strip().casefold() == str(option).strip().casefold()
        ),
        None,
    )
    try:
        service.answer(session_id, pending.get("question_id", ""), text, selected)
    except KeyError:
        _LOG.info("surface answer ignored: question no longer pending", exc_info=True)
        return False
    clear_pending_question(session_id)
    return True


# --- the run itself ----------------------------------------------------------


def _outcome_from_result(result: Mapping, session_id: str) -> RunOutcome:
    cost = result.get("cost") or {}
    total = cost.get("total_cost_usd") if isinstance(cost, Mapping) else None
    return RunOutcome(
        session_id=session_id,
        success=bool(result.get("success")),
        final_text=str(
            result.get("final_text") or "The run finished without a final response."
        ),
        outcome=result.get("outcome"),
        total_cost_usd=total,
    )


def _run_and_deliver(
    adapter: SurfaceAdapter,
    service: Any,
    command: Command,
    target: ReplyTarget,
) -> None:
    adapter.deliver_ack(target, "On it...")
    try:
        # Whatever the entry declares, a run started by a message from a
        # connected workspace spends no more than the surface cap.
        overrides = quota.cost_clamp(service, command.catalog_ref)
        if not adapter.capabilities().hitl:
            # No channel to ask a question on: a run that stops to ask would
            # hang until the process exits, so it must not be able to.
            overrides["tools"] = {"ask_user": {"enabled": False}}
        session_id = service.submit(
            command.task,
            overrides,
            catalog_ref=command.catalog_ref,
            origin=target.as_dict(),
        )
        register_target(session_id, adapter.name, target)
        result = service.result(session_id)
        adapter.deliver_result(target, _outcome_from_result(result, session_id))
    except Exception:
        _LOG.exception("surface run failed", extra={"surface": adapter.name})
        adapter.deliver_error(
            target,
            "I couldn't complete that run. Check the fabri server logs for details.",
        )


def dispatch(
    adapter: SurfaceAdapter,
    service: Any,
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    run_in_background: bool = True,
) -> tuple[int, str, dict[str, str]]:
    """Handle one inbound delivery, end to end.

    Always answers fast: surfaces expect an ack within seconds, so the run
    happens on its own thread and the result is delivered when it lands.
    """
    verified = adapter.verify(raw_body, headers)
    if isinstance(verified, ShortCircuit):
        return verified.status, verified.body, verified.headers
    payload = verified

    if _is_duplicate(adapter.name, adapter.delivery_id(payload)):
        return _OK

    decision = adapter.classify(payload)

    if isinstance(decision, ShortCircuit):
        return decision.status, decision.body, decision.headers
    if isinstance(decision, (Handled, Ignore)):
        return _OK
    if isinstance(decision, HitlAnswer):
        if deliver_answer(service, decision.target, decision.text):
            # Confirm on the surface: a human who answered a question needs to
            # see that the run actually took it.
            adapter.deliver_ack(decision.target, "Got it.")
        return _OK
    if not isinstance(decision, Dispatch):
        return _OK

    command, target = decision.command, decision.target

    if command.verb in ("list", "help"):
        adapter.deliver_error(target, render_catalog(service))
        return _OK

    catalog = getattr(service, "catalog", None) or {}
    if command.catalog_ref is not None and command.catalog_ref not in catalog:
        adapter.deliver_error(
            target,
            render_catalog(
                service, intro=f"I don't know `{command.catalog_ref}`."
            ),
        )
        return _OK

    # Quota is checked here, before anything is launched, so no adapter can
    # forget it and no surface can spend past its share.
    verdict = quota.check(service, (adapter.name, target.tenant.tenant_id))
    if not verdict.allowed:
        adapter.deliver_error(target, verdict.reason)
        return _OK

    if run_in_background:
        Thread(
            target=_run_and_deliver,
            args=(adapter, service, command, target),
            daemon=True,
        ).start()
    else:
        _run_and_deliver(adapter, service, command, target)
    return _OK
