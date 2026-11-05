"""Run-side client for the credential broker that ``fabri serve`` runs.

Under the service a run does not hold stored tokens. An http or openapi
source that names a ``secret_ref`` sends a request spec over a Unix socket
to the broker in the service process, which checks it against the run's
bound declarations, asks a human when the request class calls for it,
resolves the credential on that call, sends the request and returns the
redacted tool result.

Protocol: one connection per request, one UTF-8 JSON line in, one JSON line
out. The client never sends a rendered URL for ``op: http``. It sends the
tool name, the method and the raw path parameters, and the broker renders
the URL from the template it bound at launch. The client never sends header
specs either, only literal extra headers.

The client keeps its write side open until the reply arrives. The broker
peeks the socket before it sends an approved request and treats end of file
as "the caller left", so a half close here would cancel every approved call.

The capability key is never logged, never put in an exception and never
shown by ``repr``.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import socket
import time
from collections.abc import Callable, Mapping
from typing import Any

from fabri.tools.result import tool_error

logger = logging.getLogger("fabri.tools.credential_broker_client")

SOCKET_ENV = "FABRI_CRED_BROKER_SOCKET"
KEY_ENV = "FABRI_CRED_BROKER_KEY"
BROKER_ENV_NAMES = frozenset({SOCKET_ENV, KEY_ENV})
PROTOCOL_VERSION = 1

# The broker refuses a request line longer than this, so the client refuses
# to send one rather than wait for that answer.
MAX_REQUEST_BYTES = 1_048_576
# The longest reply line the client reads.
MAX_RESPONSE_BYTES = 8 * 1_048_576
# Slack added on top of the send timeout and the approval timeout.
_WAIT_SLACK_S = 5.0
_RECV_CHUNK = 65_536

Sender = Callable[..., dict]


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached or gave an unusable reply. The
    message holds an error type or a short reason, never a key or a value."""


def _approval_timeout_default() -> float:
    # Imported here so this module stays free of the registry import chain.
    from fabri.tools.approver import _timeout_from_env

    return _timeout_from_env()


class BrokerClient:
    """Talks to one run's credential broker binding."""

    def __init__(self, socket_path: str, key: str, *, approval_timeout_s: float | None = None) -> None:
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError("credential broker socket path is empty")
        if not isinstance(key, str) or not key:
            raise ValueError("credential broker key is empty")
        self.socket_path = socket_path
        self._key = key
        if approval_timeout_s is None:
            approval_timeout_s = _approval_timeout_default()
        self.approval_timeout_s = float(approval_timeout_s)

    def __repr__(self) -> str:
        return f"BrokerClient(socket_path={self.socket_path!r})"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BrokerClient | None:
        """A client for the broker named in the environment, or None unless
        both broker variables are set."""
        env = os.environ if env is None else env
        path = env.get(SOCKET_ENV) or ""
        key = env.get(KEY_ENV) or ""
        if not path or not key:
            return None
        return cls(path, key)

    # Public calls

    def sender(self, source: str, tool: str | None = None) -> Sender:
        """A callable with the ``http_call`` sender keyword shape, bound to
        one source (and optionally one tool). The ``url`` and ``floor_url``
        the caller passes are not sent: the broker renders the URL from the
        template it bound, using ``path_params``."""

        def send_via_broker(
            method: str,
            url: str = "",
            *,
            path_params: Mapping[str, Any] | None = None,
            params: Mapping[str, Any] | None = None,
            json_body: Any = None,
            form_body: Mapping[str, Any] | None = None,
            extra_headers: Mapping[str, str] | None = None,
            timeout_s: float = 30.0,
            tool_name: str = "",
            floor_url: str | None = None,
        ) -> dict:
            return self.send(
                source=source, tool=tool or tool_name, method=method, url=url,
                path_params=path_params, params=params, json_body=json_body, form_body=form_body,
                extra_headers=extra_headers, timeout_s=timeout_s, tool_name=tool_name or tool or "",
                floor_url=floor_url,
            )

        send_via_broker.broker_source = source  # type: ignore[attr-defined]
        return send_via_broker

    def send(
        self,
        *,
        source: str,
        tool: str,
        method: str,
        url: str = "",
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        form_body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        tool_name: str = "",
        floor_url: str | None = None,
    ) -> dict:
        """Ask the broker to make one http request for a bound tool.

        Returns the broker's reply dict as it came (the same shape
        ``http_call.send`` returns, already redacted, plus ``v``). A
        transport problem comes back as a tool error, not an exception.
        ``url`` and ``floor_url`` are accepted for the sender shape and
        ignored. Raises ValueError, before any I/O, when both a JSON and a
        form body are given."""
        del url, floor_url  # the broker renders the URL from its own template
        if json_body is not None and form_body is not None:
            raise ValueError("give json_body or form_body, not both")
        label = tool_name or tool or source
        request = {
            "v": PROTOCOL_VERSION,
            "key": self._key,
            "op": "http",
            "source": source,
            "tool": tool,
            "method": str(method).upper(),
            "path_params": dict(path_params) if path_params else {},
            "params": dict(params) if params else None,
            "json": json_body,
            "form": dict(form_body) if form_body is not None else None,
            "extra_headers": dict(extra_headers) if extra_headers else {},
            "timeout_s": float(timeout_s),
        }
        try:
            reply = self._exchange(request, float(timeout_s))
        except BrokerUnavailable as exc:
            logger.info("tool %s: credential broker unreachable (%s)", label, exc)
            return tool_error(f"{label}: credential broker unreachable ({exc})",
                              result={"status": "error", "code": None})
        return reply

    def post_mcp(
        self,
        *,
        source: str,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]:
        """Send one JSON-RPC body to an MCP source through the broker.

        Returns (status, response headers, body). Raises BrokerUnavailable
        on a transport problem and, with the broker's text, on a refusal."""
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("post_mcp body must be bytes")
        request = {
            "v": PROTOCOL_VERSION,
            "key": self._key,
            "op": "mcp",
            "source": source,
            "url": url,
            "extra_headers": dict(headers or {}),
            "body_b64": base64.b64encode(bytes(body)).decode("ascii"),
            "timeout_s": float(timeout_s),
        }
        reply = self._exchange(request, float(timeout_s))
        if reply.get("ok") is not True:
            error = reply.get("error")
            raise BrokerUnavailable(str(error) if error else "credential broker refused the request")
        status = reply.get("status")
        resp_headers = reply.get("headers")
        raw = reply.get("body_b64")
        if not isinstance(status, int) or isinstance(status, bool):
            raise BrokerUnavailable("reply has no status code")
        if not isinstance(resp_headers, Mapping) or not isinstance(raw, str):
            raise BrokerUnavailable("reply is missing headers or body")
        try:
            data = base64.b64decode(raw.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            raise BrokerUnavailable("reply body is not base64") from None
        return status, {str(k): str(v) for k, v in resp_headers.items()}, data

    # Transport

    def _wait_s(self, timeout_s: float) -> float:
        return max(float(timeout_s), 0.0) + max(self.approval_timeout_s, 0.0) + _WAIT_SLACK_S

    def _exchange(self, request: Mapping[str, Any], timeout_s: float) -> dict:
        """Write one request line and read one reply line. Raises
        BrokerUnavailable on every failure."""
        try:
            line = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise BrokerUnavailable(f"request could not be encoded ({type(exc).__name__})") from None
        if len(line) > MAX_REQUEST_BYTES:
            raise BrokerUnavailable("request too large")
        deadline = time.monotonic() + self._wait_s(timeout_s)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                sock.settimeout(_remaining(deadline))
                sock.connect(self.socket_path)
                sock.sendall(line)
                # No shutdown(SHUT_WR) here: the broker reads end of file as
                # the caller leaving and drops an approved request.
                raw = _read_line(sock, deadline)
            except BrokerUnavailable:
                raise
            except (OSError, ValueError) as exc:
                name = "TimeoutError" if isinstance(exc, socket.timeout) else type(exc).__name__
                raise BrokerUnavailable(name) from None
        finally:
            try:
                sock.close()
            except OSError:
                pass
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BrokerUnavailable("reply is not JSON") from None
        if not isinstance(reply, dict):
            raise BrokerUnavailable("reply is not a JSON object")
        if reply.get("v") != PROTOCOL_VERSION or isinstance(reply.get("v"), bool):
            raise BrokerUnavailable("reply has an unknown protocol version")
        if not isinstance(reply.get("ok"), bool):
            raise BrokerUnavailable("reply has no ok flag")
        return reply


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("deadline passed")
    return left


def _read_line(sock: socket.socket, deadline: float) -> bytes:
    """Read up to the first newline, at most MAX_RESPONSE_BYTES."""
    buf = bytearray()
    while True:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(_RECV_CHUNK)
        if not chunk:
            raise BrokerUnavailable("connection closed before a full reply")
        idx = chunk.find(b"\n")
        if idx >= 0:
            if len(buf) + idx > MAX_RESPONSE_BYTES:
                raise BrokerUnavailable("reply too large")
            buf += chunk[:idx]
            return bytes(buf)
        buf += chunk
        if len(buf) > MAX_RESPONSE_BYTES:
            raise BrokerUnavailable("reply too large")
