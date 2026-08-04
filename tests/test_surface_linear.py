"""Linear as the third surface.

Same shape as the GitHub tests: the pipeline's guarantees are already covered
against a fake adapter, so what is left here is Linear's own wire format —
signature, replay window, the label trigger, and delivery.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from fabri.service.surfaces import pipeline
from fabri.service.surfaces.linear import LABEL_PREFIX, LinearAdapter
from fabri.service.surfaces.types import (
    Dispatch,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    TenantRef,
)

from tests.test_surface_pipeline import FakeService

SECRET = "linear-hook-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    pipeline.reset_state()
    yield
    pipeline.reset_state()


def _signed(payload: dict, *, secret: str = SECRET):
    raw = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"Linear-Signature": signature}


def _issue_payload(labels: list[str], **kw) -> dict:
    return {
        "type": kw.get("type", "Issue"),
        "action": kw.get("action", "update"),
        "organizationId": "org-1",
        "webhookId": "wh-1",
        "webhookTimestamp": kw.get("timestamp", int(time.time() * 1000)),
        "data": {
            "id": "issue-uuid",
            "identifier": "ENG-42",
            "title": "Login redirects to a blank page",
            "description": "Repro: sign in on Safari.",
            "labels": [{"name": name} for name in labels],
        },
    }


def test_forged_signature_is_rejected():
    adapter = LinearAdapter(FakeService())
    raw, headers = _signed(_issue_payload([f"{LABEL_PREFIX}triage"]), secret="wrong")

    verified = adapter.verify(raw, headers)

    assert isinstance(verified, ShortCircuit)
    assert verified.status == 401


def test_a_replayed_delivery_is_rejected():
    """Linear stamps the body; an old delivery must not start a second run."""
    adapter = LinearAdapter(FakeService())
    stale = int((time.time() - 3600) * 1000)
    raw, headers = _signed(_issue_payload([f"{LABEL_PREFIX}triage"], timestamp=stale))

    verified = adapter.verify(raw, headers)

    assert isinstance(verified, ShortCircuit)
    assert verified.status == 401


def test_labelled_issue_dispatches_with_the_issue_as_the_task():
    adapter = LinearAdapter(FakeService())
    raw, headers = _signed(_issue_payload([f"{LABEL_PREFIX}triage", "bug"]))

    decision = adapter.classify(adapter.verify(raw, headers))

    assert isinstance(decision, Dispatch)
    assert decision.command.catalog_ref == "triage"
    assert "ENG-42: Login redirects to a blank page" in decision.command.task
    assert "Repro: sign in on Safari." in decision.command.task
    assert decision.target.tenant.tenant_id == "org-1"
    assert decision.target.locator["issue_id"] == "issue-uuid"


def test_an_unlabelled_issue_is_ignored():
    adapter = LinearAdapter(FakeService())
    raw, headers = _signed(_issue_payload(["bug", "p1"]))

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_non_issue_events_are_ignored():
    adapter = LinearAdapter(FakeService())
    raw, headers = _signed(_issue_payload([f"{LABEL_PREFIX}triage"], type="Comment"))

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_delivery_id_includes_the_timestamp():
    """Same webhook, different delivery: the id must distinguish them, or a
    later legitimate run would be swallowed as a duplicate."""
    adapter = LinearAdapter(FakeService())
    first = adapter.delivery_id(_issue_payload([], timestamp=1))
    second = adapter.delivery_id(_issue_payload([], timestamp=2))

    assert first != second


def test_linear_declares_no_hitl():
    assert LinearAdapter().capabilities().hitl is False


def test_result_comments_on_the_issue_with_the_workspace_token(
    monkeypatch: pytest.MonkeyPatch,
):
    class Store:
        def get_token(self, workspace_id: str) -> str | None:
            return "lin_api_workspace" if workspace_id == "org-1" else None

    service = FakeService()
    service.linear_install_store = Store()  # type: ignore[attr-defined]
    adapter = LinearAdapter(service)
    posted: list[tuple] = []
    monkeypatch.setattr(
        "fabri.integrations.linear.comment_issue",
        lambda issue_id, body, token, **kw: posted.append((issue_id, body, token)),
    )

    target = ReplyTarget(TenantRef("linear", "org-1"), {"issue_id": "issue-uuid"})
    assert adapter.deliver_result(
        target,
        RunOutcome(session_id="s1", success=True, final_text="Triaged: auth cookie",
                   total_cost_usd=0.02),
    )

    issue_id, body, token = posted[0]
    assert (issue_id, token) == ("issue-uuid", "lin_api_workspace")
    assert body.startswith("```") and "Triaged: auth cookie" in body
    assert "$0.0200" in body


def test_a_workspace_we_have_no_token_for_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    """Never fall through to another tenant's credentials.

    The env credential belongs to whatever workspace the operator connected by
    hand. Using it for an unknown organisation would post one tenant's run into
    another tenant's workspace, so a named workspace with no install must fail.
    """
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_the_servers_own_workspace")

    class Store:
        def get_token(self, workspace_id: str) -> str | None:
            return None

    service = FakeService()
    service.linear_install_store = Store()  # type: ignore[attr-defined]
    adapter = LinearAdapter(service)

    assert adapter._token(
        ReplyTarget(TenantRef("linear", "org-unknown"), {"issue_id": "i"})
    ) is None
    assert not adapter.deliver_error(
        ReplyTarget(TenantRef("linear", "org-unknown"), {"issue_id": "i"}), "nope"
    )


def test_single_tenant_deployment_still_uses_its_env_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    """No workspace id in play at all: the operator's own token is correct."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_single_tenant")
    adapter = LinearAdapter(FakeService())

    token = adapter._token(ReplyTarget(TenantRef("linear", None), {"issue_id": "i"}))

    assert token == "lin_api_single_tenant"
