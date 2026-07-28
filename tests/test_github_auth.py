"""Offline tests for GitHub PAT and App authentication."""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

from fabri.repo import github_auth


pytestmark = pytest.mark.unit


def _configure_app_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    pem_path = tmp_path / "github-app.pem"
    pem_path.write_text("fake private key", encoding="utf-8")
    monkeypatch.setenv("FABRI_CRED_GITHUB_APP_ID", "123")
    monkeypatch.setenv("FABRI_CRED_GITHUB_INSTALLATION_ID", "456")
    monkeypatch.setenv("FABRI_CRED_GITHUB_PRIVATE_KEY", str(pem_path))
    return pem_path


def _install_fake_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_jwt = ModuleType("jwt")

    def encode(
        claims: dict[str, object],
        private_key: str,
        algorithm: str,
    ) -> str:
        assert claims["iss"] == "123"
        assert claims["exp"] == claims["iat"] + 600
        assert private_key == "fake private key"
        assert algorithm == "RS256"
        return "fake.jwt"

    setattr(fake_jwt, "encode", encode)
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)


def _fake_response(token: str = "ghs_faketoken") -> io.BytesIO:
    return io.BytesIO(
        json.dumps(
            {
                "token": token,
                "expires_at": "2999-01-01T00:00:00Z",
            }
        ).encode("utf-8")
    )


def test_app_auth_caches_and_refreshes_installation_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_app_credentials(monkeypatch, tmp_path)
    _install_fake_jwt(monkeypatch)
    validated_urls: list[str] = []
    requests: list[urllib.request.Request] = []

    def fake_validate_url(url: str) -> str:
        validated_urls.append(url)
        return url

    def fake_open(
        request: urllib.request.Request,
        timeout: int,
    ) -> io.BytesIO:
        assert timeout == 30
        requests.append(request)
        return _fake_response()

    monkeypatch.setattr(github_auth, "validate_url", fake_validate_url)
    monkeypatch.setattr(github_auth._opener, "open", fake_open)

    auth = github_auth.AppAuth()
    assert auth.get_token() == "ghs_faketoken"
    assert auth.get_token() == "ghs_faketoken"
    assert len(requests) == 1

    auth._expires_at = time.time() + 30
    assert auth.get_token() == "ghs_faketoken"
    assert len(requests) == 2

    expected_url = (
        "https://api.github.com/app/installations/456/access_tokens"
    )
    assert validated_urls == [expected_url, expected_url]
    assert requests[0].full_url == expected_url
    assert requests[0].method == "POST"
    assert requests[0].get_header("Authorization") == "Bearer fake.jwt"
    assert requests[0].get_header("Accept") == "application/vnd.github+json"


def test_app_auth_threads_store_into_every_secret_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pem_path = _configure_app_credentials(monkeypatch, tmp_path)
    _install_fake_jwt(monkeypatch)
    store = object()
    calls: list[tuple[str, object | None]] = []
    values = {
        "github:app_id": "123",
        "github:installation_id": "456",
        "github:private_key": str(pem_path),
    }

    def fake_resolve_secret(ref: str, supplied_store: object | None) -> str:
        calls.append((ref, supplied_store))
        return values[ref]

    monkeypatch.setattr(github_auth, "resolve_secret", fake_resolve_secret)
    monkeypatch.setattr(github_auth, "validate_url", lambda url: url)
    monkeypatch.setattr(
        github_auth._opener,
        "open",
        lambda request, timeout: _fake_response(),
    )

    assert github_auth.AppAuth(store).get_token() == "ghs_faketoken"
    assert calls == [
        ("github:app_id", store),
        ("github:installation_id", store),
        ("github:private_key", store),
    ]


def test_build_github_auth_selects_app_or_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object()
    monkeypatch.setenv("FABRI_CRED_GITHUB_APP_ID", "123")
    app_auth = github_auth.build_github_auth(store)
    assert isinstance(app_auth, github_auth.AppAuth)
    assert app_auth._store is store

    monkeypatch.delenv("FABRI_CRED_GITHUB_APP_ID")
    pat_auth = github_auth.build_github_auth(store)
    assert isinstance(pat_auth, github_auth.PatAuth)
    assert pat_auth._store is store


def test_pat_auth_resolves_default_token_with_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object()
    calls: list[tuple[str, object | None]] = []

    def fake_resolve_secret(ref: str, supplied_store: object | None) -> str:
        calls.append((ref, supplied_store))
        return "github-pat"

    monkeypatch.setattr(github_auth, "resolve_secret", fake_resolve_secret)

    assert github_auth.PatAuth(store).get_token() == "github-pat"
    assert calls == [("github:default", store)]


def test_app_auth_redacts_tokens_from_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_app_credentials(monkeypatch, tmp_path)
    _install_fake_jwt(monkeypatch)
    monkeypatch.setattr(github_auth, "validate_url", lambda url: url)

    def fake_open(
        request: object,
        timeout: int,
    ) -> io.BytesIO:
        raise RuntimeError(
            "request rejected fake.jwt and ghs_sensitive_cached_token"
        )

    monkeypatch.setattr(github_auth._opener, "open", fake_open)
    auth = github_auth.AppAuth()
    auth._token = "ghs_sensitive_cached_token"
    auth._expires_at = time.time() + 30

    with pytest.raises(RuntimeError) as caught:
        auth.get_token()

    detail = str(caught.value)
    assert "fake.jwt" not in detail
    assert "ghs_sensitive_cached_token" not in detail
    assert detail.count("[REDACTED]") == 2
