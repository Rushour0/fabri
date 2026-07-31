"""Unit tests for GitHub App webhook event handling."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from fabri.service.github_events import handle_github_event
from fabri.service.install_store import GitHubInstallStore


pytestmark = pytest.mark.unit


class _Svc:
    pass


def _service(tmp_path) -> _Svc:
    service = _Svc()
    service.github_install_store = GitHubInstallStore(
        tmp_path / "installs.db"
    )
    return service


def _signature(secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()


def _headers(secret: str, raw_body: bytes, event: str) -> dict[str, str]:
    return {
        "X-Hub-Signature-256": _signature(secret, raw_body),
        "X-GitHub-Event": event,
    }


def test_signature_must_match_exact_posted_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", secret)
    service = _service(tmp_path)
    raw = json.dumps({"action": "ignored"}).encode()
    signed_headers = _headers(secret, raw, "push")

    assert handle_github_event(raw, signed_headers, service) == (200, "", {})

    differently_encoded = raw + b" "
    assert handle_github_event(
        differently_encoded, signed_headers, service
    ) == (401, "", {})

    tampered = bytearray(raw)
    tampered[-2] ^= 1
    assert handle_github_event(
        bytes(tampered), signed_headers, service
    ) == (401, "", {})


def test_created_installation_upserts_account_and_repos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", secret)
    service = _service(tmp_path)
    payload = {
        "action": "created",
        "installation": {
            "id": 456,
            "account": {"login": "acme", "type": "Organization"},
        },
        "repositories": [{"full_name": "acme/app"}],
    }
    raw = json.dumps(payload).encode()

    result = handle_github_event(
        raw, _headers(secret, raw, "installation"), service
    )

    assert result == (200, "", {})
    install = service.github_install_store.get("456")
    assert install is not None
    assert install["account_login"] == "acme"
    assert install["account_type"] == "Organization"
    assert install["repos"] == ["acme/app"]


def test_deleted_installation_removes_seeded_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", secret)
    service = _service(tmp_path)
    service.github_install_store.upsert(installation_id="456")
    payload = {"action": "deleted", "installation": {"id": 456}}
    raw = json.dumps(payload).encode()

    result = handle_github_event(
        raw, _headers(secret, raw, "installation"), service
    )

    assert result == (200, "", {})
    assert service.github_install_store.get("456") is None


def test_installation_repositories_merges_seeded_repos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", secret)
    service = _service(tmp_path)
    service.github_install_store.upsert(
        installation_id="456",
        account_login="acme",
        repos=["acme/a", "acme/b"],
    )
    payload = {
        "installation": {"id": 456},
        "repositories_added": [{"full_name": "acme/c"}],
        "repositories_removed": [{"full_name": "acme/a"}],
    }
    raw = json.dumps(payload).encode()

    result = handle_github_event(
        raw,
        _headers(secret, raw, "installation_repositories"),
        service,
    )

    assert result == (200, "", {})
    install = service.github_install_store.get("456")
    assert install is not None
    assert install["repos"] == ["acme/b", "acme/c"]


def test_unknown_event_returns_200_without_changing_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", secret)
    service = _service(tmp_path)
    service.github_install_store.upsert(
        installation_id="456",
        account_login="acme",
        repos=["acme/app"],
        now=1000.0,
    )
    before = service.github_install_store.list()
    raw = json.dumps({"ref": "refs/heads/main"}).encode()

    result = handle_github_event(
        raw, _headers(secret, raw, "push"), service
    )

    assert result == (200, "", {})
    assert service.github_install_store.list() == before


def test_bad_json_with_valid_signature_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret = "webhook-secret"
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", secret)
    service = _service(tmp_path)
    raw = b"{not json"

    assert handle_github_event(
        raw, _headers(secret, raw, "installation"), service
    ) == (400, "", {})

