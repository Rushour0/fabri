"""Jira as a surface.

The pipeline's guarantees are proved against a fake adapter in
test_surface_pipeline. Here the Jira adapter is driven through the same
``pipeline.dispatch`` to show it keeps them (bad signature refused, a repeated
delivery runs once, a label naming no catalog entry starts nothing), and then
Jira's own wire format is checked: the label trigger, the replay window, the
site check, and a result comment that only goes out through the connector's
always_approve tool. No live Jira call is made.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time

import httpx
import pytest

from fabri.service.surfaces import pipeline
from fabri.service.surfaces.jira import (
    DELIVERY_HEADER,
    LABEL_PREFIX,
    SIGNATURE_HEADER,
    JiraAdapter,
    verify_jira_signature,
)
from fabri.service.surfaces.registry import SurfaceRegistry
from fabri.service.surfaces.types import (
    Dispatch,
    Ignore,
    ReplyTarget,
    RunOutcome,
    ShortCircuit,
    TenantRef,
)
from tests.test_surface_pipeline import FakeService

pytestmark = pytest.mark.unit

SECRET = "jira-hook-secret"
SITE = "https://acme.atlassian.net"
EMAIL = "bot@example.com"
API_TOKEN = "jira-token"
BASIC_AUTH = "Basic Ym90QGV4YW1wbGUuY29tOmppcmEtdG9rZW4="


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("JIRA_URL", SITE)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", API_TOKEN)
    for name in ("JIRA_WORKSPACE", "JIRA_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    pipeline.reset_state()
    yield
    pipeline.reset_state()


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(201, json={"id": "30001"})


def _signed(payload: dict, *, secret: str = SECRET, delivery: str = "d-1"):
    raw = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {SIGNATURE_HEADER: f"sha256={digest}", DELIVERY_HEADER: delivery}


def _created(labels: list[str], **kw) -> dict:
    return {
        "timestamp": kw.get("timestamp", int(time.time() * 1000)),
        "webhookEvent": kw.get("event", "jira:issue_created"),
        "issue": {
            "id": "10012",
            "key": "PROJ-12",
            "self": kw.get("self", f"{SITE}/rest/api/2/issue/10012"),
            "fields": {
                "summary": "Login redirects to a blank page",
                "description": kw.get("description", "Repro: sign in on Safari."),
                "labels": labels,
            },
        },
    }


def _label_added(before: list[str], after: list[str]) -> dict:
    payload = _created(after, event="jira:issue_updated")
    payload["changelog"] = {
        "items": [
            {"field": "labels", "fromString": " ".join(before), "toString": " ".join(after)}
        ]
    }
    return payload


def _post(adapter, service, payload, **kw):
    raw, headers = _signed(payload, **kw)
    return pipeline.dispatch(adapter, service, raw, headers, run_in_background=False)


# --- the pipeline's guarantees, held by the Jira adapter ---------------------


def test_full_loop_label_to_run_to_approved_comment():
    recorder = Recorder()
    service = FakeService()
    calls: list[str] = []
    adapter = JiraAdapter(
        service,
        approver=lambda name, args, manifest: calls.append(name) or True,
        transport=recorder.transport,
    )

    status, _, _ = _post(adapter, service, _created([f"{LABEL_PREFIX}triage", "bug"]))

    assert status == 200
    [submission] = service.submissions
    assert submission["catalog_ref"] == "triage"
    assert submission["task"].startswith("PROJ-12: Login redirects to a blank page")
    assert submission["origin"]["surface"] == "jira"
    assert submission["overrides"]["tools"] == {"ask_user": {"enabled": False}}
    # Only the result comment asks for approval: the adapter sends no ack.
    assert calls == ["jira_add_comment"]
    [request] = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == f"{SITE}/rest/api/3/issue/PROJ-12/comment"
    assert request.headers["authorization"] == BASIC_AUTH
    body = json.loads(request.content)["body"]
    assert body["type"] == "doc" and body["content"][0]["type"] == "codeBlock"
    assert body["content"][0]["content"][0]["text"] == "Done"
    assert body["content"][1]["content"][0]["text"] == "Cost: $0.0123"


def test_bad_signature_is_refused_before_anything_happens():
    service = FakeService()
    adapter = JiraAdapter(service, approver=lambda *a: True, transport=Recorder().transport)

    status, _, _ = _post(adapter, service, _created([f"{LABEL_PREFIX}triage"]), secret="wrong")

    assert status == 401
    assert service.submissions == []


@pytest.mark.parametrize(
    "header",
    ["", "deadbeef", "sha1=deadbeef", "sha256="],
    ids=["missing", "no-scheme", "wrong-scheme", "empty-digest"],
)
def test_malformed_signature_headers_fail_closed(header):
    raw = json.dumps(_created([f"{LABEL_PREFIX}triage"])).encode()
    verified = JiraAdapter(FakeService()).verify(raw, {SIGNATURE_HEADER: header})
    assert isinstance(verified, ShortCircuit) and verified.status == 401


def test_no_secret_configured_refuses_every_delivery(monkeypatch: pytest.MonkeyPatch):
    raw, headers = _signed(_created([f"{LABEL_PREFIX}triage"]))
    monkeypatch.delenv("JIRA_WEBHOOK_SECRET")
    verified = JiraAdapter(FakeService()).verify(raw, headers)
    assert isinstance(verified, ShortCircuit) and verified.status == 401


def test_repeated_delivery_id_runs_once():
    service = FakeService()
    adapter = JiraAdapter(service)
    payload = _created([f"{LABEL_PREFIX}triage"])

    _post(adapter, service, payload, delivery="same-id")
    _post(adapter, service, payload, delivery="same-id")

    assert len(service.submissions) == 1


def test_different_delivery_ids_each_run():
    service = FakeService()
    adapter = JiraAdapter(service)
    payload = _created([f"{LABEL_PREFIX}triage"])

    _post(adapter, service, payload, delivery="one")
    _post(adapter, service, payload, delivery="two")

    assert len(service.submissions) == 2


def test_label_naming_no_catalog_entry_is_ignored():
    recorder = Recorder()
    service = FakeService()
    adapter = JiraAdapter(service, approver=lambda *a: True, transport=recorder.transport)

    status, _, _ = _post(adapter, service, _created([f"{LABEL_PREFIX}not-in-catalog"]))

    assert status == 200
    assert service.submissions == []
    # Ignored outright: no catalog listing is posted back to the issue either.
    assert recorder.requests == []


# --- Jira's own wire format ---------------------------------------------------


def test_signature_helper_matches_hmac_sha256_hex():
    raw = b'{"a": 1}'
    digest = hmac.new(b"k", raw, hashlib.sha256).hexdigest()
    assert verify_jira_signature("k", raw, f"sha256={digest}")
    assert verify_jira_signature("k", raw, f"SHA256={digest.upper()}")
    assert not verify_jira_signature("k", raw + b" ", f"sha256={digest}")


def test_a_stale_delivery_is_refused():
    stale = int((time.time() - 3600) * 1000)
    raw, headers = _signed(_created([f"{LABEL_PREFIX}triage"], timestamp=stale))
    verified = JiraAdapter(FakeService()).verify(raw, headers)
    assert isinstance(verified, ShortCircuit) and verified.status == 401


def test_update_that_adds_the_label_dispatches():
    adapter = JiraAdapter(FakeService())
    raw, headers = _signed(_label_added(["bug"], ["bug", f"{LABEL_PREFIX}triage"]))

    decision = adapter.classify(adapter.verify(raw, headers))

    assert isinstance(decision, Dispatch)
    assert decision.command.catalog_ref == "triage"
    assert decision.target.locator == {"issue_key": "PROJ-12", "issue_id": "10012"}
    assert decision.target.tenant == TenantRef("jira", "acme.atlassian.net")


def test_update_to_an_already_labelled_issue_is_ignored():
    """Editing a labelled issue must not rerun the entry."""
    adapter = JiraAdapter(FakeService())
    label = f"{LABEL_PREFIX}triage"
    raw, headers = _signed(_label_added([label], [label, "p1"]))

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_unlabelled_issue_and_other_events_are_ignored():
    adapter = JiraAdapter(FakeService())
    for payload in (
        _created(["bug"]),
        _created([f"{LABEL_PREFIX}triage"], event="comment_created"),
        _created([f"{LABEL_PREFIX}triage"], event="jira:issue_deleted"),
    ):
        raw, headers = _signed(payload)
        assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_adf_description_becomes_plain_task_text():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Step one"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Step two"}]},
        ],
    }
    adapter = JiraAdapter(FakeService())
    raw, headers = _signed(_created([f"{LABEL_PREFIX}triage"], description=adf))

    decision = adapter.classify(adapter.verify(raw, headers))

    assert isinstance(decision, Dispatch)
    assert "Step one\nStep two" in decision.command.task


def test_issue_on_another_site_is_ignored():
    adapter = JiraAdapter(FakeService())
    payload = _created([f"{LABEL_PREFIX}triage"], self="https://other.atlassian.net/rest/api/2/issue/1")
    raw, headers = _signed(payload)

    assert isinstance(adapter.classify(adapter.verify(raw, headers)), Ignore)


def test_jira_declares_no_hitl_and_routes_on_its_path():
    adapter = JiraAdapter()
    assert adapter.capabilities().hitl is False
    registry = SurfaceRegistry()
    registry.register(adapter)
    assert registry.routes()["/jira/webhook"] is adapter
    assert registry.get("jira") is adapter


# --- the comment write is approval-gated -------------------------------------


def _target() -> ReplyTarget:
    return ReplyTarget(TenantRef("jira", "acme.atlassian.net"), {"issue_key": "PROJ-12"})


def _outcome() -> RunOutcome:
    return RunOutcome(session_id="s1", success=True, final_text="Triaged", total_cost_usd=None)


def test_no_approver_sends_nothing():
    recorder = Recorder()
    adapter = JiraAdapter(FakeService(), transport=recorder.transport)

    assert adapter.deliver_result(_target(), _outcome()) is False
    assert adapter.deliver_error(_target(), "nope") is False
    assert recorder.requests == []


def test_declined_approval_sends_nothing():
    recorder = Recorder()
    seen: list[tuple] = []

    def approver(name, args, manifest):
        seen.append((name, manifest.approval, args["key"]))
        return False

    adapter = JiraAdapter(FakeService(), approver=approver, transport=recorder.transport)

    assert adapter.deliver_result(_target(), _outcome()) is False
    assert seen == [("jira_add_comment", "always_approve", "PROJ-12")]
    assert recorder.requests == []


def test_oauth_base_comments_through_the_stored_connection(monkeypatch: pytest.MonkeyPatch):
    oauth_base = "https://api.atlassian.com/ex/jira/cloud-123"
    monkeypatch.setenv("JIRA_URL", oauth_base)
    monkeypatch.setenv("JIRA_ACCESS_TOKEN", "jira-access-token")
    recorder = Recorder()
    adapter = JiraAdapter(FakeService(), approver=lambda *a: True, transport=recorder.transport)

    assert adapter.deliver_result(_target(), _outcome()) is True
    [request] = recorder.requests
    assert str(request.url) == f"{oauth_base}/rest/api/3/issue/PROJ-12/comment"
    assert request.headers["authorization"] == "Bearer jira-access-token"


def test_long_result_is_cut_to_fit_the_approval_card():
    recorder = Recorder()
    adapter = JiraAdapter(FakeService(), approver=lambda *a: True, transport=recorder.transport)
    outcome = RunOutcome(session_id="s1", success=True, final_text="x" * 50_000)

    assert adapter.deliver_result(_target(), outcome) is True
    text = json.loads(recorder.requests[0].content)["body"]["content"][0]["content"][0]["text"]
    assert len(text) < 20_000 and text.endswith("[cut: the full result is in the fabri run]")


def test_no_jira_url_sends_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JIRA_URL")
    recorder = Recorder()
    adapter = JiraAdapter(FakeService(), approver=lambda *a: True, transport=recorder.transport)

    assert adapter.deliver_result(_target(), _outcome()) is False
    assert recorder.requests == []
