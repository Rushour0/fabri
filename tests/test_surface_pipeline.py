"""The surface contract, proved without any real surface.

If the pipeline can be driven end to end by an adapter that imports nothing
from Slack, GitHub, or Linear, then the contract is sufficient by construction
and the next integration is additive. That is the whole claim of the design, so
it gets its own test file rather than living inside a Slack test.
"""
from __future__ import annotations

import json
import time

import pytest

from fabri.service.surfaces import pipeline
from fabri.service.surfaces.base import SurfaceAdapter
from fabri.service.surfaces.registry import SurfaceRegistry
from fabri.service.surfaces.types import (
    Command,
    Dispatch,
    Handled,
    HitlAnswer,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    SurfaceCapabilities,
    TenantRef,
)


class FakeAdapter(SurfaceAdapter):
    """A surface with no wire protocol: JSON in, list of deliveries out."""

    name = "fake"
    webhook_path = "/fake/webhook"

    def __init__(self, *, hitl: bool = True, secret: str = "s3cret") -> None:
        self.secret = secret
        self._hitl = hitl
        self.delivered: list[tuple[str, str]] = []
        self.lifecycle: list[dict] = []

    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(hitl=self._hitl, threads=True)

    def verify(self, raw_body, headers):
        if headers.get("X-Fake-Signature") != self.secret:
            return ShortCircuit(401, "invalid signature")
        return json.loads(raw_body)

    def delivery_id(self, payload):
        return payload.get("id")

    def classify(self, payload):
        target = ReplyTarget(
            TenantRef(self.name, payload.get("tenant")), {"room": payload.get("room")}
        )
        kind = payload.get("kind")
        if kind == "uninstall":
            self.lifecycle.append(payload)
            return Handled()
        if kind == "answer":
            return HitlAnswer(target, payload.get("text", ""))
        if kind != "command":
            return Ignore()
        command = pipeline.parse_command(payload.get("text", ""))
        if command is None:
            return Ignore()
        return Dispatch(command, target)

    def deliver_ack(self, target, text):
        self.delivered.append(("ack", text))
        return True

    def deliver_result(self, target, outcome: RunOutcome):
        cost = "" if outcome.total_cost_usd is None else f" ${outcome.total_cost_usd}"
        self.delivered.append(("result", outcome.final_text + cost))
        return True

    def deliver_error(self, target, text):
        self.delivered.append(("error", text))
        return True

    def deliver_question(self, target, question):
        self.delivered.append(("question", question.get("question", "")))
        return True


class FakeService:
    """Just enough service for the pipeline: a catalog, submit, result, answer."""

    def __init__(self) -> None:
        self.catalog = {
            "release-notes": {"kind": "agency", "meta": {"title": "Release notes"}},
            "triage": {"kind": "agency", "meta": {"title": "Bug triage"}},
        }
        self.submissions: list[dict] = []
        self.answers: list[tuple] = []

    def submit(self, task, overrides=None, *, catalog_ref=None, origin=None, **kw):
        self.submissions.append(
            {
                "task": task,
                "overrides": overrides,
                "catalog_ref": catalog_ref,
                "origin": origin,
            }
        )
        return f"sess-{len(self.submissions)}"

    def result(self, session_id):
        return {
            "session_id": session_id,
            "success": True,
            "final_text": "Done",
            "cost": {"total_cost_usd": 0.0123},
        }

    def answer(self, session_id, question_id, text, selected_option=None):
        self.answers.append((session_id, question_id, text, selected_option))


def _post(adapter, service, payload, *, signature="s3cret"):
    return pipeline.dispatch(
        adapter,
        service,
        json.dumps(payload).encode(),
        {"X-Fake-Signature": signature},
        run_in_background=False,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    pipeline.reset_state()
    yield
    pipeline.reset_state()


# --- the grammar -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("run release-notes summarize the week", Command("run", "summarize the week", "release-notes")),
        ("RUN triage  the login bug ", Command("run", "the login bug", "triage")),
        ("run release-notes", Command("run", "", "release-notes")),
        ("list", Command("list")),
        ("help", Command("help")),
        ("just chatting", None),
        ("", None),
    ],
)
def test_command_grammar_is_shared_and_deterministic(text, expected):
    assert pipeline.parse_command(text) == expected


# --- the loop ----------------------------------------------------------------


def test_full_loop_verify_dispatch_submit_deliver():
    adapter, service = FakeAdapter(), FakeService()

    status, _, _ = _post(
        adapter,
        service,
        {"id": "d1", "kind": "command", "tenant": "T1", "room": "R1",
         "text": "run release-notes summarize the week"},
    )

    assert status == 200
    assert service.submissions[0]["catalog_ref"] == "release-notes"
    assert service.submissions[0]["task"] == "summarize the week"
    # The run knows where it came from.
    assert service.submissions[0]["origin"] == {
        "surface": "fake", "tenant_id": "T1", "locator": {"room": "R1"},
    }
    assert ("ack", "On it...") in adapter.delivered
    assert ("result", "Done $0.0123") in adapter.delivered


def test_forged_signature_is_rejected_before_anything_happens():
    adapter, service = FakeAdapter(), FakeService()

    status, body, _ = _post(
        adapter, service,
        {"id": "d1", "kind": "uninstall", "tenant": "T1"},
        signature="wrong",
    )

    assert (status, body) == (401, "invalid signature")
    assert adapter.lifecycle == []
    assert service.submissions == []


def test_duplicate_delivery_runs_once():
    adapter, service = FakeAdapter(), FakeService()
    payload = {"id": "same", "kind": "command", "tenant": "T1", "room": "R1",
               "text": "run triage the login bug"}

    _post(adapter, service, payload)
    _post(adapter, service, payload)

    assert len(service.submissions) == 1


def test_unknown_ref_answers_with_the_catalog_and_runs_nothing():
    adapter, service = FakeAdapter(), FakeService()

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run nope do a thing"})

    assert service.submissions == []
    kind, text = adapter.delivered[0]
    assert kind == "error"
    assert "I don't know `nope`" in text
    assert "release-notes" in text and "triage" in text


def test_list_lists_without_running():
    adapter, service = FakeAdapter(), FakeService()

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "list"})

    assert service.submissions == []
    assert "release-notes" in adapter.delivered[0][1]


def test_lifecycle_and_noise_are_handled_without_running():
    adapter, service = FakeAdapter(), FakeService()

    _post(adapter, service, {"id": "d1", "kind": "uninstall", "tenant": "T1"})
    _post(adapter, service, {"id": "d2", "kind": "reaction", "tenant": "T1"})

    assert len(adapter.lifecycle) == 1
    assert service.submissions == []
    assert adapter.delivered == []


def test_hitl_round_trip_question_out_answer_back():
    adapter, service = FakeAdapter(), FakeService()
    target = ReplyTarget(TenantRef("fake", "T1"), {"room": "R1"})
    pipeline.register_target("sess-1", "fake", target)

    # The run asks; the surface carries it.
    assert adapter.deliver_question(target, {"question": "Which format?"})
    pipeline.set_pending_question(
        "sess-1", {"question_id": "q1", "options": ["Brief", "Detailed"]}
    )

    # A human replies on the surface; the pipeline routes it into the run.
    _post(adapter, service, {"id": "d9", "kind": "answer", "tenant": "T1",
                             "room": "R1", "text": "detailed"})

    assert service.answers == [("sess-1", "q1", "detailed", "Detailed")]
    assert ("ack", "Got it.") in adapter.delivered
    assert pipeline.take_pending_question("sess-1") is None


def test_surface_without_hitl_disables_ask_user():
    """A surface that cannot ask a question must not launch a run that can:
    it would block until the process dies."""
    adapter, service = FakeAdapter(hitl=False), FakeService()

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run triage the login bug"})

    assert service.submissions[0]["overrides"]["tools"] == {
        "ask_user": {"enabled": False}
    }


def test_hitl_surface_leaves_ask_user_alone():
    adapter, service = FakeAdapter(hitl=True), FakeService()

    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run triage the login bug"})

    # A HITL surface keeps ask_user; the cost clamp still applies (see
    # tests/test_surface_quota.py).
    assert "tools" not in service.submissions[0]["overrides"]


def test_a_failing_run_reports_back_instead_of_going_silent():
    adapter, service = FakeAdapter(), FakeService()

    def boom(*a, **k):
        raise RuntimeError("provider down")

    service.submit = boom  # type: ignore[method-assign]
    _post(adapter, service, {"id": "d1", "kind": "command", "tenant": "T1",
                             "room": "R1", "text": "run triage the login bug"})

    assert adapter.delivered[-1][0] == "error"


def test_background_dispatch_returns_immediately():
    """Surfaces expect an ack in seconds, so the run cannot be on this path."""
    adapter, service = FakeAdapter(), FakeService()
    slow = {"done": False}

    def slow_result(session_id):
        time.sleep(0.2)
        slow["done"] = True
        return {"success": True, "final_text": "Done", "cost": {}}

    service.result = slow_result  # type: ignore[method-assign]
    started = time.monotonic()
    status, _, _ = pipeline.dispatch(
        adapter,
        service,
        json.dumps({"id": "d1", "kind": "command", "tenant": "T1", "room": "R1",
                    "text": "run triage the login bug"}).encode(),
        {"X-Fake-Signature": "s3cret"},
    )
    elapsed = time.monotonic() - started

    assert status == 200
    assert elapsed < 0.15, "dispatch waited for the run"
    time.sleep(0.4)
    assert slow["done"]


# --- the registry ------------------------------------------------------------


def test_registry_routes_by_path_and_name():
    registry = SurfaceRegistry()
    adapter = registry.register(FakeAdapter())

    assert registry.get("fake") is adapter
    assert registry.routes()["/fake/webhook"] is adapter
    assert registry.routes()["/fake/webhook/"] is adapter
    assert registry.get("discord") is None


def test_registry_rejects_a_nameless_adapter():
    class Nameless(FakeAdapter):
        name = ""

    with pytest.raises(ValueError):
        SurfaceRegistry().register(Nameless())
