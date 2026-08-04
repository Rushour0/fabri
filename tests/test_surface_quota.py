"""Limits on runs started from a connected tool.

A browser run is one a human clicked. A surface run arrives whenever anyone in
a connected workspace types, so volume and spend have to be bounded before the
run is launched — and bounded from the run store, so a restart is not a way to
get a fresh budget.
"""
from __future__ import annotations

import json
import time

import pytest

from fabri.service.surfaces import pipeline, quota
from fabri.service.surfaces.quota import QuotaPolicy

from tests.test_surface_pipeline import FakeAdapter, FakeService, _post


class FakeRunStore:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_runs(self, limit: int = 2000) -> list[dict]:
        return list(self._rows[:limit])


def _row(**kw) -> dict:
    row = {
        "session_id": kw.get("session_id", "s"),
        "submitted_at": kw.get("submitted_at", time.time()),
        "finished_at": kw.get("finished_at", time.time()),
        "cost_total": kw.get("cost_total", 0.0),
        "origin": json.dumps(
            {
                "surface": kw.get("surface", "fake"),
                "tenant_id": kw.get("tenant_id", "T1"),
                "locator": {},
            }
        ),
    }
    return row


def _service_with(rows: list[dict]) -> FakeService:
    service = FakeService()
    service.run_store = FakeRunStore(rows)  # type: ignore[attr-defined]
    return service


STRICT = QuotaPolicy(
    max_concurrent=1, max_runs_per_day=3, max_usd_per_day=1.0,
    global_usd_per_day=5.0, max_cost_per_run=0.75,
)


def test_allows_a_tenant_under_every_limit():
    service = _service_with([_row(cost_total=0.1)])
    assert quota.check(service, ("fake", "T1"), policy=STRICT).allowed


def test_blocks_a_second_concurrent_run():
    service = _service_with([_row(finished_at=None)])

    verdict = quota.check(service, ("fake", "T1"), policy=STRICT)

    assert not verdict.allowed
    assert "in progress" in verdict.reason


def test_blocks_on_runs_per_day():
    service = _service_with([_row(session_id=str(i)) for i in range(3)])

    verdict = quota.check(service, ("fake", "T1"), policy=STRICT)

    assert not verdict.allowed
    assert "24 hours" in verdict.reason


def test_blocks_on_spend_per_day():
    service = _service_with([_row(cost_total=0.6), _row(cost_total=0.5)])

    verdict = quota.check(service, ("fake", "T1"), policy=STRICT)

    assert not verdict.allowed
    assert "$1.10" in verdict.reason


def test_blocks_on_the_global_budget_even_for_a_quiet_tenant():
    rows = [_row(tenant_id="T-OTHER", cost_total=5.0)]
    service = _service_with(rows)

    verdict = quota.check(service, ("fake", "T1"), policy=STRICT)

    assert not verdict.allowed
    assert "daily budget" in verdict.reason


def test_one_tenant_cannot_spend_another_tenants_budget():
    """Counters are per tenant: a noisy workspace must not lock out a quiet one."""
    rows = [_row(tenant_id="T-NOISY", session_id=str(i)) for i in range(3)]
    service = _service_with(rows)

    assert quota.check(service, ("fake", "T-QUIET"), policy=STRICT).allowed
    assert not quota.check(service, ("fake", "T-NOISY"), policy=STRICT).allowed


def test_runs_outside_the_window_do_not_count():
    old = time.time() - 90000  # 25 hours ago
    rows = [_row(session_id=str(i), submitted_at=old, cost_total=5.0) for i in range(9)]

    assert quota.check(_service_with(rows), ("fake", "T1"), policy=STRICT).allowed


def test_browser_runs_are_not_counted_against_a_surface():
    """Runs with no origin came from Studio and are somebody's deliberate click."""
    rows = [{"session_id": str(i), "submitted_at": time.time(), "cost_total": 9.0,
             "finished_at": None, "origin": None} for i in range(9)]

    assert quota.check(_service_with(rows), ("fake", "T1"), policy=STRICT).allowed


def test_policy_reads_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FABRI_SURFACE_MAX_CONCURRENT", "4")
    monkeypatch.setenv("FABRI_SURFACE_MAX_USD_PER_DAY", "12.5")
    monkeypatch.setenv("FABRI_SURFACE_MAX_RUNS_PER_DAY", "not-a-number")

    policy = QuotaPolicy.from_env()

    assert policy.max_concurrent == 4
    assert policy.max_usd_per_day == 12.5
    # Garbage falls back to the safe default rather than disabling the limit.
    assert policy.max_runs_per_day == quota.DEFAULT_MAX_RUNS_PER_DAY


def test_a_service_without_a_run_store_is_allowed_not_crashed():
    assert quota.check(FakeService(), ("fake", "T1"), policy=STRICT).allowed


# --- the clamp ---------------------------------------------------------------


def test_clamp_caps_an_expensive_entry():
    service = FakeService()
    service.catalog["big-company"] = {"kind": "company", "meta": {"max_cost_usd": 20}}

    assert quota.cost_clamp(service, "big-company", STRICT) == {
        "agent": {"max_cost_usd": 0.75}
    }


def test_clamp_keeps_a_cheaper_entrys_own_ceiling():
    service = FakeService()
    service.catalog["thrifty"] = {"kind": "agency", "meta": {"max_cost_usd": 0.15}}

    assert quota.cost_clamp(service, "thrifty", STRICT) == {
        "agent": {"max_cost_usd": 0.15}
    }


def test_clamp_applies_when_an_entry_declares_nothing():
    assert quota.cost_clamp(FakeService(), "release-notes", STRICT) == {
        "agent": {"max_cost_usd": 0.75}
    }


# --- through the pipeline ----------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    pipeline.reset_state()
    yield
    pipeline.reset_state()


def test_pipeline_refuses_over_quota_before_submitting(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter()
    service = _service_with([_row(finished_at=None)])
    monkeypatch.setattr(QuotaPolicy, "from_env", classmethod(lambda cls: STRICT))

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run triage the login bug"})

    assert service.submissions == []
    kind, text = adapter.delivered[-1]
    assert kind == "error"
    assert "in progress" in text


def test_pipeline_applies_the_clamp_to_a_permitted_run(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter()
    service = _service_with([])
    monkeypatch.setattr(QuotaPolicy, "from_env", classmethod(lambda cls: STRICT))

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run triage the login bug"})

    assert service.submissions[0]["overrides"]["agent"]["max_cost_usd"] == 0.75


def test_clamp_and_ask_user_disable_compose(monkeypatch: pytest.MonkeyPatch):
    """A non-HITL surface gets both overrides, not one instead of the other."""
    adapter = FakeAdapter(hitl=False)
    service = _service_with([])
    monkeypatch.setattr(QuotaPolicy, "from_env", classmethod(lambda cls: STRICT))

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run triage the login bug"})

    overrides = service.submissions[0]["overrides"]
    assert overrides["agent"]["max_cost_usd"] == 0.75
    assert overrides["tools"] == {"ask_user": {"enabled": False}}
