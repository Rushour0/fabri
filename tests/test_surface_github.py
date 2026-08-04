"""GitHub as the second surface: the additivity claim, tested.

Everything the pipeline guarantees is asserted in test_surface_pipeline.py
against a fake adapter. What remains for a real adapter is its own wire
format — signature, event shape, command extraction, delivery — which is what
this file covers. If that ratio holds for the next surface too, the abstraction
is doing its job.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from fabri.service.surfaces import pipeline
from fabri.service.surfaces.github import GitHubAdapter
from fabri.service.surfaces.types import Dispatch, Handled, Ignore, ShortCircuit

from tests.test_surface_pipeline import FakeService

SECRET = "hook-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", SECRET)
    pipeline.reset_state()
    yield
    pipeline.reset_state()


def _signed(payload: dict, event: str, *, secret: str = SECRET, delivery: str = "d1"):
    raw = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


def _comment_payload(body: str, *, user_type: str = "User") -> dict:
    return {
        "action": "created",
        "comment": {"body": body, "user": {"type": user_type}},
        "issue": {"number": 42},
        "repository": {"full_name": "acme/widgets"},
        "installation": {"id": 12345},
    }


def test_forged_signature_is_rejected():
    adapter = GitHubAdapter(FakeService())
    raw, headers = _signed(_comment_payload("/fabri run triage x"), "issue_comment",
                           secret="wrong-secret")

    verified = adapter.verify(raw, headers)

    assert isinstance(verified, ShortCircuit)
    assert verified.status == 401


def test_issue_comment_command_dispatches_with_repo_and_installation():
    adapter = GitHubAdapter(FakeService())
    raw, headers = _signed(
        _comment_payload("/fabri run triage the login bug"), "issue_comment"
    )

    decision = adapter.classify(adapter.verify(raw, headers))

    assert isinstance(decision, Dispatch)
    assert decision.command.catalog_ref == "triage"
    assert decision.command.task == "the login bug"
    assert decision.target.tenant.tenant_id == "12345"
    assert decision.target.locator == {"repo": "acme/widgets", "issue_number": 42}


def test_our_own_comment_never_starts_a_run():
    """A result comment that re-triggers a run is a loop with a bill attached."""
    adapter = GitHubAdapter(FakeService())
    raw, headers = _signed(
        _comment_payload("/fabri run triage x", user_type="Bot"), "issue_comment"
    )

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


@pytest.mark.parametrize(
    "body", ["just a normal comment", "fabri run triage x", "/fabri chat about it"]
)
def test_ordinary_comments_are_ignored(body: str):
    adapter = GitHubAdapter(FakeService())
    raw, headers = _signed(_comment_payload(body), "issue_comment")

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_edited_and_deleted_comments_are_ignored():
    adapter = GitHubAdapter(FakeService())
    payload = _comment_payload("/fabri run triage x")
    payload["action"] = "edited"
    raw, headers = _signed(payload, "issue_comment")

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_installation_events_are_handled_not_dispatched():
    class Store:
        def __init__(self) -> None:
            self.upserts: list[dict] = []

        def upsert(self, **kw) -> None:
            self.upserts.append(kw)

        def get(self, iid):
            return None

        def delete(self, iid):
            return True

    service = FakeService()
    service.github_install_store = Store()  # type: ignore[attr-defined]
    adapter = GitHubAdapter(service)
    payload = {
        "action": "created",
        "installation": {"id": 999, "account": {"login": "acme", "type": "Organization"}},
        "repositories": [{"full_name": "acme/widgets"}],
    }
    raw, headers = _signed(payload, "installation")

    decision = adapter.classify(adapter.verify(raw, headers))

    assert isinstance(decision, Handled)
    assert service.github_install_store.upserts[0]["installation_id"] == "999"


def test_delivery_id_comes_from_the_header():
    adapter = GitHubAdapter(FakeService())
    raw, headers = _signed(_comment_payload("/fabri list"), "issue_comment",
                           delivery="unique-delivery")

    assert adapter.delivery_id(adapter.verify(raw, headers)) == "unique-delivery"


def test_github_declares_no_hitl():
    """No way to ask a question mid-run, so the pipeline must disable ask_user."""
    assert GitHubAdapter().capabilities().hitl is False


def test_result_is_fenced_and_carries_cost(monkeypatch: pytest.MonkeyPatch):
    from fabri.service.surfaces.types import ReplyTarget, RunOutcome, TenantRef

    adapter = GitHubAdapter(FakeService())
    posted: list[tuple] = []
    monkeypatch.setattr(adapter, "_token", lambda target: "ghs-token")
    monkeypatch.setattr(
        "fabri.repo.github.comment_issue",
        lambda repo, token, number, body: posted.append((repo, token, number, body)),
    )

    target = ReplyTarget(TenantRef("github", "12345"),
                         {"repo": "acme/widgets", "issue_number": 42})
    adapter.deliver_result(
        target,
        RunOutcome(session_id="s1", success=True, final_text="@everyone done",
                   total_cost_usd=0.0123),
    )

    repo, token, number, body = posted[0]
    assert (repo, token, number) == ("acme/widgets", "ghs-token", 42)
    # Fenced, so agent output cannot @-mention its way across an organisation.
    assert body.startswith("```") and "@everyone done" in body
    assert "$0.0123" in body


def test_a_missing_installation_token_fails_closed(monkeypatch: pytest.MonkeyPatch):
    from fabri.service.surfaces.types import ReplyTarget, TenantRef

    adapter = GitHubAdapter(FakeService())
    monkeypatch.setattr(adapter, "_token", lambda target: None)

    assert not adapter.deliver_error(
        ReplyTarget(TenantRef("github", None), {"repo": "acme/widgets", "issue_number": 1}),
        "nope",
    )
