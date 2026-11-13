"""OAuth on the command line: a one-shot callback server on the loopback address.

``run_loopback_flow`` is what ``fabri connect gmail`` runs. It binds an HTTP
server to 127.0.0.1 (never a public address) on a free port, starts the
authorization code flow with ``http://127.0.0.1:<port>/callback`` as the
redirect URI, hands the authorize URL to ``open_url`` (the browser), and
waits for one callback whose ``state`` matches the one it issued. The browser
gets a plain page saying the tab can be closed; the code is then traded for a
token (with PKCE) and the connection is stored through
``OAuthCodeProvider.finish``, which checks the signed state again and takes
the pending row once.

Only connectors whose ``OAuthSpec.loopback`` is true take this path. Slack
and Linear need a registered HTTPS redirect and are connected through
``GET /connect/<provider>`` on ``fabri serve``.

Nothing here logs or prints a request line: the callback URL carries the
authorization code. Error messages name the step only.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx

from fabri.tools.connections import ConnectError
from fabri.tools.connections.oauth import OAuthCodeProvider
from fabri.tools.connector_catalog import Connector

if TYPE_CHECKING:
    from fabri.service.connection_store import ConnectionStore

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/callback"
# How long one browser connection may take to send its request.
_REQUEST_TIMEOUT_S = 10.0
_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,60}")

_DONE_PAGE = (b"<!doctype html><html><head><meta charset=\"utf-8\"><title>fabri</title></head>"
              b"<body><p>fabri got the answer. You can close this tab and go back to the terminal.</p>"
              b"</body></html>")


def _loopback_host(host: str) -> int:
    """The address family for `host`, which must be a loopback IP literal."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConnectError("the callback server needs a loopback IP address such as 127.0.0.1") from None
    if not address.is_loopback:
        raise ConnectError("the callback server listens on a loopback address only")
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


class _Callback:
    """What the one callback brought back. Filled once, under a lock."""

    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None


def _handler_class(callback: _Callback, host_header: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fabri"
        sys_version = ""
        timeout = _REQUEST_TIMEOUT_S

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib signature
            # The request line carries the authorization code; never log it.
            return

        def _answer(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 -- stdlib name
            parts = urlsplit(self.path)
            if parts.path != CALLBACK_PATH:
                self._answer(404, b"not found")
                return
            if self.headers.get("Host", "") != host_header:
                self._answer(400, b"wrong host")
                return
            query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items() if v}
            state = query.get("state", "")
            # Only the browser that went through our authorize URL knows the
            # state; anything else on this machine poking the port is refused
            # and the wait goes on.
            if not state or not hmac.compare_digest(state.encode(), callback.expected_state.encode()):
                self._answer(400, b"this callback does not match the sign-in fabri started")
                return
            with callback.lock:
                if callback.done.is_set():
                    self._answer(410, b"fabri already took a callback; you can close this tab")
                    return
                callback.state = state
                callback.code = query.get("code") or None
                error = query.get("error")
                if error:
                    callback.error = error if _ERROR_CODE_RE.fullmatch(error) else "error"
                callback.done.set()
            self._answer(200, _DONE_PAGE, "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 -- stdlib name
            self._answer(405, b"method not allowed")

    return Handler


def _server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> HTTPServer:
    family = _loopback_host(host)

    class LoopbackServer(HTTPServer):
        address_family = family
        allow_reuse_address = False

        def server_bind(self) -> None:
            # HTTPServer.server_bind looks up the host name; a loopback
            # literal needs no lookup.
            self.socket.bind(self.server_address)
            self.server_address = self.socket.getsockname()[:2]
            self.server_name = host
            self.server_port = self.server_address[1]

    try:
        return LoopbackServer((host, port), handler, bind_and_activate=True)
    except OSError as exc:
        raise ConnectError(f"could not listen on {host}:{port} ({type(exc).__name__})") from None


def _state_from_url(url: str) -> str:
    values = parse_qs(urlsplit(url).query).get("state") or []
    if not values or not values[0]:
        raise ConnectError("the authorize URL carried no state")
    return values[0]


def run_loopback_flow(store: ConnectionStore, entry: Connector, *, handle: str, scope_kind: str,
                      scopes: Sequence[str] | None, open_url: Callable[[str], object],
                      transport: httpx.BaseTransport | None = None, timeout_s: float = 300.0,
                      host: str = "127.0.0.1", port: int = 0, now: float | None = None) -> dict:
    """Run the OAuth code flow through a browser and a loopback callback.

    Returns the stored connection row (no token columns). For
    ``scope_kind == "user"`` the handle is the user id the connection belongs
    to; the host decides what an actor id is. Raises ConnectError when the
    entry has no loopback OAuth, when the vendor reports an error, when no
    callback arrives within `timeout_s`, or when the exchange fails. The
    pending row left behind by a timeout expires on its own."""
    spec = entry.oauth
    if spec is None:
        raise ConnectError(f"{entry.name} connects with a token, not OAuth")
    if not spec.loopback:
        raise ConnectError(f"{entry.name} OAuth needs the server: open GET /connect/{entry.name} on fabri serve")
    if timeout_s <= 0:
        raise ConnectError("the timeout must be more than 0 seconds")
    provider = OAuthCodeProvider(entry, transport=transport)
    if not provider.configured():
        raise ConnectError(f"{entry.name} OAuth is not configured; set {spec.client_id_env}")
    _loopback_host(host)

    # The Host header a browser sends for the redirect URI.
    holder: dict[str, str] = {}
    callback = _Callback(expected_state="")

    def handler_factory(*args: Any, **kwargs: Any) -> BaseHTTPRequestHandler:
        return _handler_class(callback, holder["host_header"])(*args, **kwargs)

    server = _server(host, port, handler_factory)  # type: ignore[arg-type]
    bound_port = server.server_port
    url_host = f"[{host}]" if ":" in host else host
    holder["host_header"] = f"{url_host}:{bound_port}"
    redirect_uri = f"http://{url_host}:{bound_port}{CALLBACK_PATH}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              name=f"fabri-oauth-{entry.name}", daemon=True)
    try:
        user_id = handle if scope_kind == "user" else None
        authorize_url = provider.begin(store, redirect_uri=redirect_uri, scope_kind=scope_kind,
                                       handle=handle, user_id=user_id, scopes=scopes, now=now)
        callback.expected_state = _state_from_url(authorize_url)
        thread.start()
        open_url(authorize_url)
        deadline = time.monotonic() + timeout_s
        while not callback.done.wait(timeout=min(0.5, max(0.0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                raise ConnectError(f"no callback within {timeout_s:g} s")
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
    if callback.error:
        raise ConnectError(f"{entry.name} sign-in was not completed ({callback.error})")
    if not callback.code or not callback.state:
        raise ConnectError(f"{entry.name} callback carried no authorization code")
    return provider.finish(store, state=callback.state, code=callback.code, now=now)


__all__ = ["CALLBACK_PATH", "run_loopback_flow"]
