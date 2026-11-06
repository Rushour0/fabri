"""Focused tests for the shared SSRF guards and canonical fetch recipe."""

from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import httpcore
import httpx
import pytest

from fabri.tools.security.ssrf import (
    PinnedNetworkBackend,
    PinnedTransport,
    ValidatingRedirect,
    host_is_blocked,
    resolve_public,
    validate_url,
)

pytestmark = pytest.mark.unit

RECIPE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "fabri"
    / "tools"
    / "recipes"
    / "fetch_url.py"
)


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = {
        "localhost": "127.0.0.1",
        "public.example": "93.184.216.34",
    }

    def getaddrinfo(
        host: str, port: int | str | None
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        address = addresses.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        pytest.param(
            "http://localhost/admin", "private/reserved", id="localhost"
        ),
        pytest.param(
            "http://127.0.0.1/admin", "private/reserved", id="loopback"
        ),
        pytest.param(
            "http://169.254.169.254/latest/meta-data/",
            "private/reserved",
            id="link-local",
        ),
        pytest.param("http://10.0.0.1/admin", "private/reserved", id="rfc1918"),
        pytest.param("file:///etc/passwd", r"only http\(s\)", id="file-scheme"),
        pytest.param(
            "ftp://public.example/file", r"only http\(s\)", id="ftp-scheme"
        ),
    ],
)
def test_validate_url_blocks_unsafe_targets(
    fake_dns: None, url: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_url(url)


def test_validate_url_allows_public_https_host(fake_dns: None) -> None:
    url = "https://public.example/path?query=yes"

    assert host_is_blocked("public.example") is False
    assert validate_url(url) == url


def test_private_host_override_keeps_original_semantics(
    fake_dns: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FABRI_FETCH_ALLOW_PRIVATE", "1")
    url = "http://127.0.0.1/admin"

    assert host_is_blocked("127.0.0.1") is False
    assert validate_url(url) == url


def test_redirect_handler_revalidates_target(fake_dns: None) -> None:
    handler = ValidatingRedirect()
    request = urllib.request.Request("https://public.example/start")

    with pytest.raises(ValueError, match="private/reserved"):
        handler.redirect_request(
            request,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )


def test_recipe_import_delegates_to_shared_guards(fake_dns: None) -> None:
    from fabri.tools.recipes import fetch_url

    url = "https://public.example/path"
    assert fetch_url._host_is_blocked("public.example") is False
    assert fetch_url._validate(url) == url
    assert fetch_url._ValidatingRedirect is ValidatingRedirect


def test_recipe_runs_as_isolated_subprocess() -> None:
    process = subprocess.run(
        [sys.executable, "-I", str(RECIPE_PATH)],
        input=json.dumps({"url": "file:///etc/passwd"}),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert process.returncode == 1
    assert json.loads(process.stdout) == {
        "error": "refused: only http(s) supported, got 'file:///etc/passwd'"
    }
    assert process.stderr == ""


# ---------------------------------------------------------------- pinned transport

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.7"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


class AlternatingResolver:
    """Answers public, private, public, ... and counts the lookups."""

    def __init__(self, answers=((PUBLIC,), (PRIVATE,))):
        self.answers = [list(a) for a in answers]
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        answer = self.answers[len(self.calls) % len(self.answers)]
        self.calls.append((host, port))
        return answer


class FakeStream(httpcore.NetworkStream):
    def __init__(self, record: dict) -> None:
        self.record = record
        self.sent = b""
        self.replied = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self.replied:
            return b""
        self.replied = True
        return RESPONSE

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.sent += buffer
        self.record["sent"] = self.sent

    def close(self) -> None:
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.record["sni"] = server_hostname
        self.record["ssl_context"] = ssl_context
        return self

    def get_extra_info(self, info: str):
        return None


class FakeInner(httpcore.NetworkBackend):
    """Records where the pinned backend connects; never touches the network."""

    def __init__(self) -> None:
        self.connects: list[str] = []
        self.record: dict = {}

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append(host)
        return FakeStream(self.record)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise AssertionError("not used")

    def sleep(self, seconds: float) -> None:  # pragma: no cover
        pass


def _client(resolver=None, inner=None) -> httpx.Client:
    return httpx.Client(transport=PinnedTransport(resolver=resolver, inner_backend=inner), trust_env=False)


def test_pinned_transport_connects_to_the_checked_address_with_original_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    resolver, inner = AlternatingResolver(), FakeInner()
    with _client(resolver, inner) as client:
        resp = client.get("https://pinned.example/ok")
    assert resp.status_code == 200 and resp.text == "ok"
    # One lookup, and the socket went to the address that lookup returned.
    assert resolver.calls == [("pinned.example", 443)]
    assert inner.connects == [PUBLIC]
    # TLS still names the host: SNI and the certificate check use it.
    assert inner.record["sni"] == "pinned.example"
    assert inner.record["ssl_context"].check_hostname is True
    assert b"\r\nHost: pinned.example\r\n" in inner.record["sent"]


def test_pinned_transport_refuses_the_private_answer_of_an_alternating_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    resolver, inner = AlternatingResolver(), FakeInner()
    with _client(resolver, inner) as client:
        assert client.get("http://flip.example/a").status_code == 200
        with pytest.raises(httpx.ConnectError, match="private/reserved"):
            client.get("http://flip.example/b")
        assert client.get("http://flip.example/c").status_code == 200
    assert len(resolver.calls) == 3
    # The private answer never reached a connect.
    assert inner.connects == [PUBLIC, PUBLIC]


def test_pinned_transport_refuses_when_any_address_is_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    inner = FakeInner()
    for answer in ([PUBLIC, PRIVATE], ["127.0.0.1"], ["::ffff:10.0.0.1"], ["169.254.169.254"], []):
        with _client(AlternatingResolver([answer]), inner) as client:
            with pytest.raises(httpx.ConnectError, match="refused"):
                client.get("https://mixed.example/")
    assert inner.connects == []


def test_check_then_send_race_is_closed_by_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_url sees a public answer, the send's own lookup sees a
    private one: the connect is refused, not sent to the private address."""
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    answers = iter([PUBLIC, PRIVATE, PUBLIC, PRIVATE])

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    inner = FakeInner()
    url = "https://rebind.example/x"
    assert validate_url(url) == url
    with _client(None, inner) as client:
        with pytest.raises(httpx.ConnectError, match="private/reserved"):
            client.get(url)
    assert inner.connects == []


def test_pinned_backend_refuses_unix_sockets_and_honours_the_private_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PinnedNetworkBackend(AlternatingResolver([[PRIVATE]]), FakeInner())
    with pytest.raises(httpcore.ConnectError, match="Unix"):
        backend.connect_unix_socket("/tmp/x.sock")
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    with pytest.raises(ValueError, match="private/reserved"):
        resolve_public("h.example", 80, AlternatingResolver([[PRIVATE]]))
    monkeypatch.setenv("FABRI_FETCH_ALLOW_PRIVATE", "1")
    assert resolve_public("h.example", 80, AlternatingResolver([[PRIVATE]])) == [PRIVATE]


def test_pinned_transport_real_socket_keeps_the_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real connect through httpcore's own backend: the resolver sends
    pinned.test to a local server, which sees the original Host header."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Host"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("FABRI_FETCH_ALLOW_PRIVATE", "1")  # the local server is loopback
        resolver = AlternatingResolver([["127.0.0.1"]])
        with _client(resolver) as client:
            resp = client.get(f"http://pinned.test:{port}/")
        assert resp.status_code == 200
        assert seen == [f"pinned.test:{port}"]
        assert resolver.calls == [("pinned.test", port)]
    finally:
        server.shutdown()
        server.server_close()
