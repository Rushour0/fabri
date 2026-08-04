"""Spend and volume limits for runs started from a connected tool.

A run started in the browser is one a human deliberately clicked. A run started
from a chat message or a webhook is one that arrives whenever someone types --
including someone who is not you, in a workspace you connected months ago. The
same catalog entry costs the same either way, so the difference has to be
enforced here, before the run is launched.

Three limits, each answering a different failure:

* concurrency  -- one impatient user, or a retry storm, fanning out runs
* runs per day -- steady drip that never trips a concurrency limit
* spend per day -- the one that actually costs money, per tenant and overall

All three are per-*tenant* (a Slack workspace, a GitHub installation), because
the tenant is who connected and who the reply goes back to. Counters come from
the run store rather than memory, so a restart does not reset someone's budget.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

# Deliberately conservative: a public demo is the case that gets abused, and a
# self-hoster who wants more says so in the environment.
DEFAULT_MAX_CONCURRENT = 1
DEFAULT_MAX_RUNS_PER_DAY = 10
DEFAULT_MAX_USD_PER_DAY = 2.0
DEFAULT_GLOBAL_USD_PER_DAY = 10.0
# A chat message should never be able to start a $20 company run.
DEFAULT_MAX_COST_PER_RUN = 0.75

_DAY_SECONDS = 86400


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


@dataclass(frozen=True)
class QuotaPolicy:
    """The limits in force. Read from the environment so a deployment can tune
    them without a release; the defaults are the safe ones."""

    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_runs_per_day: int = DEFAULT_MAX_RUNS_PER_DAY
    max_usd_per_day: float = DEFAULT_MAX_USD_PER_DAY
    global_usd_per_day: float = DEFAULT_GLOBAL_USD_PER_DAY
    max_cost_per_run: float = DEFAULT_MAX_COST_PER_RUN

    @classmethod
    def from_env(cls) -> QuotaPolicy:
        return cls(
            max_concurrent=_env_int("FABRI_SURFACE_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT),
            max_runs_per_day=_env_int("FABRI_SURFACE_MAX_RUNS_PER_DAY", DEFAULT_MAX_RUNS_PER_DAY),
            max_usd_per_day=_env_float("FABRI_SURFACE_MAX_USD_PER_DAY", DEFAULT_MAX_USD_PER_DAY),
            global_usd_per_day=_env_float("FABRI_SURFACE_GLOBAL_USD_PER_DAY", DEFAULT_GLOBAL_USD_PER_DAY),
            max_cost_per_run=_env_float("FABRI_SURFACE_MAX_COST_PER_RUN", DEFAULT_MAX_COST_PER_RUN),
        )


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: str = ""


def _origin_runs(service: Any, since: float) -> list[dict]:
    """Recent surface-started runs, with their origin decoded.

    Reads the run store directly: it is the only record that survives a restart,
    and an in-memory counter would hand someone a fresh budget by crashing the
    process.
    """
    store = getattr(service, "run_store", None)
    if store is None:
        return []
    try:
        rows = store.list_runs(limit=2000)
    except TypeError:
        rows = store.list_runs()
    except Exception:
        return []
    out = []
    for row in rows:
        record = dict(row)
        raw_origin = record.get("origin")
        if not raw_origin:
            continue
        if (record.get("submitted_at") or 0) < since:
            continue
        try:
            record["origin"] = json.loads(raw_origin)
        except (TypeError, ValueError):
            continue
        out.append(record)
    return out


def check(
    service: Any,
    tenant_key: tuple[str, str | None],
    *,
    policy: QuotaPolicy | None = None,
    now: float | None = None,
) -> QuotaDecision:
    """May this tenant start another run right now?

    Refusals carry a reason the surface can post verbatim: someone who hits a
    limit should learn what to do about it, not that "something went wrong".
    """
    policy = policy or QuotaPolicy.from_env()
    now = now if now is not None else time.time()
    surface, tenant_id = tenant_key
    window_start = now - _DAY_SECONDS

    recent = _origin_runs(service, window_start)
    mine = [
        r
        for r in recent
        if r["origin"].get("surface") == surface
        and r["origin"].get("tenant_id") == tenant_id
    ]

    running = sum(1 for r in mine if not r.get("finished_at"))
    if running >= policy.max_concurrent:
        return QuotaDecision(
            False,
            f"You already have {running} run in progress here. "
            "I'll take the next one when it finishes.",
        )

    if len(mine) >= policy.max_runs_per_day:
        return QuotaDecision(
            False,
            f"That's {len(mine)} runs from this workspace in the last 24 hours, "
            "which is the limit. Try again tomorrow.",
        )

    spent = sum(float(r.get("cost_total") or 0.0) for r in mine)
    if spent >= policy.max_usd_per_day:
        return QuotaDecision(
            False,
            f"This workspace has spent ${spent:.2f} in the last 24 hours, "
            "which is the daily limit.",
        )

    global_spent = sum(float(r.get("cost_total") or 0.0) for r in recent)
    if global_spent >= policy.global_usd_per_day:
        return QuotaDecision(
            False,
            "The server has hit its daily budget for runs started from "
            "connected tools. Try again tomorrow.",
        )

    return QuotaDecision(True)


def cost_clamp(
    service: Any,
    catalog_ref: str | None,
    policy: QuotaPolicy | None = None,
) -> dict:
    """Per-run override capping what a surface-started run may spend.

    Takes the lower of the entry's own declared ceiling and the surface cap, so
    an agency that declares $0.15 keeps $0.15 and a $20 company run started from
    a chat message becomes the cap. ``agent.max_cost_usd`` is enforced inside the
    agent loop, so this is a real stop rather than advice.
    """
    policy = policy or QuotaPolicy.from_env()
    ceiling = policy.max_cost_per_run
    entry = (getattr(service, "catalog", None) or {}).get(catalog_ref) or {}
    declared = (entry.get("meta") or {}).get("max_cost_usd")
    try:
        if declared is not None:
            ceiling = min(ceiling, float(declared))
    except (TypeError, ValueError):
        pass
    return {"agent": {"max_cost_usd": ceiling}}
