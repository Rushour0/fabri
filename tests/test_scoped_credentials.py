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
