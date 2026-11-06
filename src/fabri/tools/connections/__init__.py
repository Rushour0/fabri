"""Connections: a stored credential for one connector, keyed ``provider:handle``.

This package holds the code that makes a connection. Wave 3 has the static
path (a pasted token or a Cakemail PAT made by ``cakemail_login``); the OAuth
providers land in ``oauth.py`` and ``loopback.py`` later.

Rules every function here keeps:

- A token is a local value. It goes to the store and into one request
  header, and nowhere else: no message, log line or exception carries it.
- Error messages name the provider, the step and the HTTP status only,
  never a response body (a vendor may echo the credential back).
- A token that fails verification is not stored.

Tests swap the HTTP transport through the module attribute ``_transport``
(an ``httpx.BaseTransport`` or None for the real network). The CLI reads it
at call time, so ``monkeypatch.setattr("fabri.tools.connections._transport",
httpx.MockTransport(...))`` reaches every request ``fabri connect`` makes.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from fabri.tools.connector_catalog import Connector

if TYPE_CHECKING:  # the service package is imported lazily by callers
    from fabri.service.connection_store import ConnectionStore

DEFAULT_HANDLE = "main"
# Same shape as the --workspace check in fabri.setups.tool_entries, so every
# handle made here can be named in `fabri setups tools add connector`.
HANDLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
HANDLE_RULE = ("a handle is 1 to 80 characters of letters, digits, '_', '.' or '-', "
               "starting with a letter or digit")
# The credential store reads handle "default" from the environment only, so a
# row stored under it would never be used.
RESERVED_HANDLES = frozenset({"default"})

# Test hook: an httpx transport used by every request this package makes
# when the caller passes none.
_transport: httpx.BaseTransport | None = None


class ConnectError(ValueError):
    """A connection could not be made. The message never carries a credential."""


def handle_ok(handle: str) -> bool:
    return isinstance(handle, str) and HANDLE_RE.fullmatch(handle) is not None


def store_provider(entry: Connector) -> str:
    """The provider name rows for this connector are stored under."""
    if not entry.secret_provider:
        raise ConnectError(f"connector {entry.name} takes no stored connection")
    return entry.secret_provider


def _client(transport: httpx.BaseTransport | None, timeout_s: float) -> httpx.Client:
    return httpx.Client(transport=transport if transport is not None else _transport,
                        follow_redirects=False, timeout=timeout_s)


def verify_token(entry: Connector, token: str, *, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 10.0) -> dict:
    """GET the connector's ``verify_path`` with the token in its auth header.
    Returns the parsed JSON body ({} when it is not a JSON object, or when the
    connector has no verify path, in which case no request is made)."""
    if not entry.verify_path or not entry.remote_url:
        return {}
    from fabri.tools.security.ssrf import validate_url

    url = entry.remote_url.rstrip("/") + entry.verify_path
    try:
        validate_url(url)
    except ValueError:
        raise ConnectError(f"{entry.name} verify URL was refused") from None
    headers = {entry.auth_header: f"{entry.auth_prefix}{token}", "Accept": "application/json"}
    try:
        with _client(transport, timeout_s) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ConnectError(f"{entry.name} verify failed ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise ConnectError(f"{entry.name} refused the token")
    if not 200 <= resp.status_code < 300:
        raise ConnectError(f"{entry.name} verify failed (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _display_name(verified: Mapping[str, Any]) -> str | None:
    """A readable account name from a verify response, when it has one."""
    for holder in (verified.get("data"), verified):
        if isinstance(holder, Mapping):
            for key in ("email", "name", "login"):
                value = holder.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:200]
    return None


def connect_static(store: ConnectionStore, entry: Connector, *, handle: str, token: str,
                   scope_kind: str = "org", display_name: str | None = None,
                   transport: httpx.BaseTransport | None = None, verify: bool = True) -> dict:
    """Store a pasted token as the connection ``<provider>:<handle>``.

    The token is checked against the vendor first unless `verify` is false;
    a refused token stores nothing. An existing row under the same name is
    replaced whole (see ``ConnectionStore.upsert``). Returns the stored row,
    which has no token columns."""
    if not handle_ok(handle):
        raise ConnectError(HANDLE_RULE)
    if handle in RESERVED_HANDLES:
        raise ConnectError(f"the handle {handle!r} is reserved; pick another, such as {DEFAULT_HANDLE}")
    if scope_kind not in ("org", "user"):
        raise ConnectError("scope must be org or user")
    if not isinstance(token, str) or not token.strip():
        raise ConnectError("the token is empty")
    token = token.strip()
    if any(ch in token for ch in "\r\n\x00"):
        raise ConnectError("the token has a line break or a NUL in it")
    provider = store_provider(entry)
    verified = verify_token(entry, token, transport=transport) if verify else {}
    store.upsert(provider=provider, handle=handle, access_token=token, scope_kind=scope_kind,
                 display_name=display_name or _display_name(verified), meta={"source": "static"})
    row = store.get(provider, handle)
    if row is None:
        raise ConnectError(f"{entry.name}: the connection was not stored")
    return row


def next_step(entry: Connector, handle: str) -> str:
    """The command that adds this connection to a setup."""
    return f"fabri setups tools add connector {entry.name} --workspace {handle} --scope org:<org>"


__all__ = ["DEFAULT_HANDLE", "HANDLE_RE", "HANDLE_RULE", "ConnectError", "connect_static", "handle_ok",
           "next_step", "store_provider", "verify_token"]
