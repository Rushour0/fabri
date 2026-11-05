"""The Cakemail password-grant helper makes a PAT with the spec-derived scopes
and keeps the password and bodies out of every error."""
from __future__ import annotations

import json
import socket
from urllib.parse import parse_qs

import httpx
import pytest

from fabri.tools.connections import ConnectError
from fabri.tools.connections.cakemail_login import CAKEMAIL_PATS_URL, CAKEMAIL_TOKEN_URL, create_pat, pat_scopes

pytestmark = pytest.mark.unit

EXPECTED_SCOPES = (
    "campaign_blueprints:read campaigns:read campaigns:send campaigns:send-test campaigns:write contacts:read "
    "contacts:write lists:read logs:read reports:read segments:read senders:read tags:read templates:read"
).split()
PASSWORD = "pw-Hunter2-unique"
ACCESS = "jwt-short-lived-unique"
PAT = "ck_pat_" + "a1" * 20


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                      ("93.184.216.34", 0))])


def recording_transport(seen, *, token_status=200, pat_status=200, token_body=None, pat_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == CAKEMAIL_TOKEN_URL:
            body = token_body if token_body is not None else {"access_token": ACCESS, "token_type": "bearer",
                                                               "expires_in": 3600, "accounts": [7]}
            return httpx.Response(token_status, json=body)
        if str(request.url) == CAKEMAIL_PATS_URL:
            body = pat_body if pat_body is not None else {"key_prefix": PAT[:12], "name": "x", "scopes": [],
                                                           "status": "active", "created_at": 1, "plaintext": PAT}
            return httpx.Response(pat_status, json=body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_pat_scopes_are_derived_from_curated_ops():
    assert pat_scopes() == tuple(sorted(EXPECTED_SCOPES))
    assert len(pat_scopes()) == 14


def test_create_pat_request_shapes():
    seen: list[httpx.Request] = []
    pat = create_pat("ops@example.com", PASSWORD, name="fabri host 2026-11-06", expires_days=30,
                     account_ids=[7, 9], transport=recording_transport(seen), now=1_000_000.0)
    assert pat == PAT
    assert [str(r.url) for r in seen] == [CAKEMAIL_TOKEN_URL, CAKEMAIL_PATS_URL]
    login, create = seen
    assert login.method == "POST"
    assert login.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = {k: v[0] for k, v in parse_qs(login.content.decode()).items()}
    assert form == {"grant_type": "password", "username": "ops@example.com", "password": PASSWORD,
                    "scopes": "user"}
    assert "authorization" not in login.headers
    assert create.method == "POST"
    assert create.headers["authorization"] == f"Bearer {ACCESS}"
    body = json.loads(create.content)
    assert body == {"name": "fabri host 2026-11-06", "scopes": list(pat_scopes()),
                    "expires_at": 1_000_000 + 30 * 86400, "allowed_account_ids": [7, 9]}


def test_create_pat_omits_account_ids_when_none_given():
    seen: list[httpx.Request] = []
    create_pat("ops@example.com", PASSWORD, name="n", transport=recording_transport(seen), now=0.0)
    body = json.loads(seen[1].content)
    assert "allowed_account_ids" not in body and body["expires_at"] == 365 * 86400


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"token_status": 401, "token_body": {"detail": f"bad password {PASSWORD}"}}, "cakemail login failed (HTTP 401)"),
    ({"pat_status": 403, "pat_body": {"detail": f"token {ACCESS} lacks rights"}},
     "cakemail create PAT failed (HTTP 403)"),
    ({"token_body": {"challenge": "mfa", "echo": PASSWORD}}, "cakemail login returned no access token"),
    ({"pat_body": {"name": "x", "echo": ACCESS}}, "cakemail create PAT returned no token"),
])
def test_create_pat_errors_name_step_and_status_only(kwargs, message):
    seen: list[httpx.Request] = []
    with pytest.raises(ConnectError) as info:
        create_pat("ops@example.com", PASSWORD, name="n", transport=recording_transport(seen, **kwargs))
    text = str(info.value)
    assert text.startswith(message)
    for secret in (PASSWORD, ACCESS, PAT, "detail", "echo"):
        assert secret not in text
    assert info.value.__cause__ is None


def test_create_pat_network_error_names_type_only():
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {PASSWORD}")
    with pytest.raises(ConnectError) as info:
        create_pat("ops@example.com", PASSWORD, name="n", transport=httpx.MockTransport(boom))
    assert str(info.value) == "cakemail login failed (ConnectError)"


def test_create_pat_never_follows_a_redirect():
    def redirect(request):
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    with pytest.raises(ConnectError) as info:
        create_pat("ops@example.com", PASSWORD, name="n", transport=httpx.MockTransport(redirect))
    assert str(info.value) == "cakemail login failed (HTTP 302)"
