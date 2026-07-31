"""Unit tests for GitHub App webhook helpers."""
from __future__ import annotations

import hashlib
import hmac

import pytest

from fabri.service import github_app


pytestmark = pytest.mark.unit


def test_verify_webhook_signature_accepts_valid_signature() -> None:
    secret = "known-secret"
    raw_body = b'{"action":"created"}'
    signature = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    assert github_app.verify_webhook_signature(secret, raw_body, signature)


def test_verify_webhook_signature_rejects_invalid_inputs() -> None:
    secret = "known-secret"
    raw_body = b'{"action":"created"}'
    signature = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    replacement = "0" if signature[-1] != "0" else "1"
    tampered_signature = signature[:-1] + replacement

    assert not github_app.verify_webhook_signature(
        secret, raw_body, tampered_signature
    )
    assert not github_app.verify_webhook_signature(
        "wrong-secret", raw_body, signature
    )
    assert not github_app.verify_webhook_signature(
        secret, raw_body, signature.removeprefix("sha256=")
    )
    assert not github_app.verify_webhook_signature(secret, raw_body, "")
    assert not github_app.verify_webhook_signature("", raw_body, signature)


def test_install_url_uses_configured_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)
    assert github_app.install_url() is None

    monkeypatch.setenv("GITHUB_APP_SLUG", "")
    assert github_app.install_url() is None

    monkeypatch.setenv("GITHUB_APP_SLUG", "my-app")
    assert (
        github_app.install_url()
        == "https://github.com/apps/my-app/installations/new"
    )

