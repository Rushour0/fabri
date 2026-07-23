"""Focused tests for the SSRF-hardened webhook POST recipe."""

from __future__ import annotations

import io
import json
import socket
import urllib.request
from email.message import Message

import pytest

from fabri.tools.recipes import webhook_post

pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(
        self,
        body: bytes = b'{"accepted":true}',
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def read(self, size: int = -1) -> bytes:
        return self._body[:size]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = {
        "localhost": "127.0.0.1",
        "hooks.example": "93.184.216.34",
    }

    def getaddrinfo(
        host: str, port: int | str | None
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        address = addresses.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: object,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(webhook_post.sys, "stdin", io.StringIO(json.dumps(args)))
    return_code = webhook_post.main()
    output = json.loads(capsys.readouterr().out)
    return return_code, output


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://localhost/admin", id="localhost"),
        pytest.param("http://127.0.0.1/admin", id="loopback"),
        pytest.param(
            "http://169.254.169.254/latest/meta-data/", id="link-local"
        ),
        pytest.param("http://10.0.0.1/admin", id="rfc1918"),
        pytest.param("file:///etc/passwd", id="file-scheme"),
    ],
)
def test_blocked_url_never_opens_request(
    fake_dns: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    def unexpected_open(*args: object, **kwargs: object) -> None:
        pytest.fail("request must not be opened for a blocked URL")

    monkeypatch.setattr(webhook_post._opener, "open", unexpected_open)

    return_code, output = invoke(
        monkeypatch, capsys, {"url": url, "payload": {}}
    )

    assert return_code == 1
    assert output["ok"] is False
    assert "refused:" in str(output["error"])


def test_happy_path_posts_json_and_returns_bounded_result(
    fake_dns: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def open_request(
        request: urllib.request.Request, *, timeout: float
    ) -> FakeResponse:
        captured.update(request=request, timeout=timeout)
        return FakeResponse(
            status=200,
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": "req-123",
                "Set-Cookie": "private=value",
            },
        )

    monkeypatch.setattr(webhook_post._opener, "open", open_request)

    return_code, output = invoke(
        monkeypatch,
        capsys,
        {
            "url": "https://hooks.example/events",
            "payload": {"event": "created", "id": 7},
        },
    )

    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert return_code == 0
    assert request.get_method() == "POST"
    assert json.loads(request.data or b"") == {"event": "created", "id": 7}
    assert request.get_header("Content-type") == "application/json"
    assert captured["timeout"] == 15
    assert output == {
        "ok": True,
        "result": {
            "status": 200,
            "body": '{"accepted":true}',
            "truncated": False,
            "headers": {
                "content-type": "application/json",
                "x-request-id": "req-123",
            },
        },
    }


def test_auth_ref_injects_bearer_header(
    fake_dns: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, urllib.request.Request] = {}
    monkeypatch.setenv("FABRI_CRED_ZAPIER_PRIMARY", "resolved-token")

    def open_request(
        request: urllib.request.Request, *, timeout: float
    ) -> FakeResponse:
        captured["request"] = request
        return FakeResponse()

    monkeypatch.setattr(webhook_post._opener, "open", open_request)

    return_code, output = invoke(
        monkeypatch,
        capsys,
        {
            "url": "https://hooks.example/events",
            "payload": {"ok": True},
            "headers": {"Authorization": "Bearer user-supplied"},
            "auth": "zapier:primary",
        },
    )

    assert return_code == 0
    assert output["ok"] is True
    assert (
        captured["request"].get_header("Authorization")
        == "Bearer resolved-token"
    )


def test_redirect_handler_revalidates_every_hop(fake_dns: None) -> None:
    handler = webhook_post._ValidatingRedirect()
    request = urllib.request.Request(
        "https://hooks.example/start",
        data=b"{}",
        method="POST",
    )

    with pytest.raises(ValueError, match="private/reserved"):
        handler.redirect_request(
            request,
            fp=None,
            code=307,
            msg="Temporary Redirect",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )


def test_resolved_secret_is_redacted_from_request_error(
    fake_dns: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "do-not-leak-this-token"
    monkeypatch.setenv("FABRI_CRED_N8N_PRODUCTION", secret)

    def fail_with_secret(
        request: urllib.request.Request, *, timeout: float
    ) -> FakeResponse:
        raise RuntimeError(f"upstream rejected Bearer {secret}")

    monkeypatch.setattr(webhook_post._opener, "open", fail_with_secret)

    return_code, output = invoke(
        monkeypatch,
        capsys,
        {
            "url": "https://hooks.example/events",
            "payload": {},
            "auth": "n8n:production",
        },
    )

    serialized = json.dumps(output)
    assert return_code == 1
    assert output["ok"] is False
    assert secret not in serialized
    assert "[REDACTED]" in str(output["error"])
