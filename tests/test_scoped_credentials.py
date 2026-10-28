"""ScopedCredentialStore: per-run grants keep tenants apart, and every
lookup is audited without the secret value."""
from __future__ import annotations

import json

import pytest

from fabri.tools.credential_store import (
    CredentialNotFoundError,
    CredentialScopeError,
    EnvCredentialStore,
    ScopedCredentialStore,
)
from fabri.tools.secret_refs import default_credential_store, resolve_secret


@pytest.fixture
def two_tenants(monkeypatch):
    # Other suites leave FABRI_INSTALL_DB set; these tests use env credentials.
    monkeypatch.delenv("FABRI_INSTALL_DB", raising=False)
    monkeypatch.setenv("FABRI_CRED_SLACK_ACME", "xoxb-acme-secret")
    monkeypatch.setenv("FABRI_CRED_SLACK_GLOBEX", "xoxb-globex-secret")
    monkeypatch.setenv("FABRI_CRED_LINEAR_ACME", "lin-acme-secret")


def test_grant_limits_handles(two_tenants):
    store = ScopedCredentialStore(EnvCredentialStore(), ["slack:acme"])
    assert resolve_secret("slack:acme", store) == "xoxb-acme-secret"
    with pytest.raises(CredentialScopeError, match="slack:globex is not granted"):
        resolve_secret("slack:globex", store)
    with pytest.raises(CredentialScopeError):
        resolve_secret("linear:acme", store)


def test_provider_wildcard(two_tenants):
    store = ScopedCredentialStore(EnvCredentialStore(), ["slack:*"])
    assert store.get("slack", "globex") == "xoxb-globex-secret"
    assert not store.allows("linear", "acme")


def test_audit_log_has_no_secret_values(two_tenants, tmp_path):
    log = tmp_path / "audit" / "creds.jsonl"
    store = ScopedCredentialStore(EnvCredentialStore(), ["slack:acme", "slack:nobody"],
                                  audit_path=log, actor="run-42")
    store.get("slack", "acme")
    with pytest.raises(CredentialScopeError):
        store.get("slack", "globex")
    with pytest.raises(CredentialNotFoundError):
        store.get("slack", "nobody")
    text = log.read_text()
    assert "secret" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert [(r["handle"], r["result"], r["actor"]) for r in rows] == [
        ("acme", "ok", "run-42"), ("globex", "denied", "run-42"), ("nobody", "missing", "run-42")]


def test_default_store_audits_when_configured(two_tenants, tmp_path, monkeypatch):
    log = tmp_path / "creds.jsonl"
    monkeypatch.delenv("FABRI_INSTALL_DB", raising=False)
    monkeypatch.setenv("FABRI_CRED_AUDIT_LOG", str(log))
    assert resolve_secret("slack:globex", default_credential_store()) == "xoxb-globex-secret"
    assert json.loads(log.read_text())["result"] == "ok"


# -- per-source grants in the tool policy --------------------------------

import httpx  # noqa: E402

from fabri.tools.policy import PolicyError, ToolPolicy  # noqa: E402
from fabri.tools.sources import build_tool_source, secret_refs  # noqa: E402


def _http_source(name: str, ref: str) -> dict:
    return {"kind": "http", "name": name, "url": "https://api.example.com/items", "method": "GET",
            "headers": {"Authorization": {"secret_ref": ref, "prefix": "Bearer "}}}


def _policy(grants) -> ToolPolicy:
    return ToolPolicy.from_data({"allowed_kinds": ["http"], "credential_grants": grants})


def test_secret_refs_lists_headers_and_env():
    decl = {"headers": {"A": {"secret_ref": "x:1"}, "B": "literal"}, "env": {"T": {"secret_ref": "y:2"}}}
    assert secret_refs(decl) == ["x:1", "y:2"]


def test_policy_refuses_a_source_using_an_ungranted_ref(two_tenants):
    policy = _policy({"acme_*": ["slack:acme"]})
    build_tool_source(_http_source("acme_items", "slack:acme"), policy=policy)
    with pytest.raises(PolicyError, match="slack:globex is not in the policy's credential_grants"):
        build_tool_source(_http_source("acme_items", "slack:globex"), policy=policy)
    with pytest.raises(PolicyError):
        build_tool_source(_http_source("other_items", "slack:acme"), policy=policy)


def test_no_grants_key_means_no_cap(two_tenants):
    policy = ToolPolicy.from_data({"allowed_kinds": ["http"]})
    assert policy.grants_for("anything") is None
    build_tool_source(_http_source("any_items", "slack:globex"), policy=policy)


def test_granted_source_sends_its_own_token(two_tenants):
    seen = []

    def handle(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"ok": True})

    [(_, handler)] = build_tool_source(_http_source("acme_items", "slack:acme"),
                                       policy=_policy({"acme_*": ["slack:*"]}),
                                       transport=httpx.MockTransport(handle))
    assert handler({})["ok"] and seen == ["Bearer xoxb-acme-secret"]


def test_bad_grants_shape_is_rejected():
    with pytest.raises(PolicyError, match="credential_grants"):
        ToolPolicy.from_data({"credential_grants": {"x": "slack:acme"}})
