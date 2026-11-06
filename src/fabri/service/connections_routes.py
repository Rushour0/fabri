"""HTTP routes for connections: list, add and remove them, and the OAuth flow.

Routes (mounted on ``fabri serve`` and ``fabri studio`` like ``SetupsApi``):

- ``GET    /connections``                     rows without tokens, plus ``mine``
- ``POST   /connections``                     body ``{provider, handle?, token, scope?, verify?}``
                                              stores a pasted token
- ``DELETE /connections?provider=&handle=``   ``{"deleted": bool}``
- ``GET    /connect/<provider>?scope=org|user``  302 to the vendor's authorize page
- ``GET    /connect/<provider>/callback``     the vendor's redirect back; stores the row
- ``GET    /slack/install``, ``/slack/oauth/callback``,
  ``/linear/install``, ``/linear/oauth/callback``
                                              the older Slack and Linear install
                                              paths, now aliases of the two above

Who may do what:

- With auth off everything is open, as the older install routes were, but a
  user-scoped connection needs a signed-in user, so it is refused (400).
  ``/connections`` keeps the token rule of ``SetupsApi``: when
  ``FABRI_SERVICE_TOKEN`` or ``FABRI_ADMIN_TOKEN`` is set, a caller without
  the token may not read or change it.
- With auth on, an org connection needs an admin. A user connection belongs
  to the signed-in user who made it: its handle is that user's id, whatever
  the request said, and only that user or an admin may remove it. A non-admin
  lists org rows and their own user rows only.
- The OAuth callback only completes for the principal that started the flow:
  the pending row's ``user_id`` must equal the caller's (None equals None
  only with auth off, or for an org flow a token started). The check runs
  before the code is exchanged.

No token, code, verifier or client secret is ever written to a response, a
log line or an exception message here. A failed exchange is logged by the
provider name and the exception type only.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fabri.core.logging_setup import get_logger

logger = get_logger()

MAX_BODY_BYTES = 64 * 1024

# Older install paths kept as aliases: path -> (provider, is_callback).
LEGACY_PATHS: dict[str, tuple[str, bool]] = {
    "/slack/install": ("slack", False),
    "/slack/oauth/callback": ("slack", True),
    "/linear/install": ("linear", False),
    "/linear/oauth/callback": ("linear", True),
}

_PROVIDER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


class _Refused(Exception):
    """A request the route refuses, with the HTTP status and a message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _PrincipalCheckedStore:
    """The connection store as ``OAuthCodeProvider.finish`` sees it.

    ``finish`` pops the pending row before it exchanges the code. This wrapper
    checks that row against the caller first, so a callback that arrives in
    another user's browser burns the nonce and never reaches the vendor.
    Every other attribute passes through to the real store.
    """

    def __init__(self, store: Any, *, user_id: str | None, is_admin: bool, auth_on: bool) -> None:
        self._store = store
        self._user_id = user_id
        self._is_admin = is_admin
        self._auth_on = auth_on

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def pending_pop(self, nonce: str, **kwargs: Any) -> dict | None:
        from fabri.tools.connections import ConnectError

        row = self._store.pending_pop(nonce, **kwargs)
        if row is None:
            return None
        if row.get("user_id") != self._user_id:
            raise ConnectError("this connection was started by someone else")
        if self._auth_on and row.get("scope_kind") == "org" and not self._is_admin:
            raise ConnectError("an org connection needs an admin")
        if row.get("scope_kind") == "user" and not row.get("user_id"):
            raise ConnectError("a user connection needs a signed-in user")
        return row


class ConnectionsApi:
    """Serves ``/connections``, ``/connect/*`` and the legacy install paths.

    ``transport`` is an ``httpx`` transport handed to the OAuth provider and
    to token verification, for tests; None means the real network.
    """

    def __init__(self, service: Any, *, transport: Any = None) -> None:
        self.service = service
        self.transport = transport

    # ---- routing

    @staticmethod
    def _route(raw_path: str) -> tuple[str, str | None, bool] | None:
        """(kind, provider, is_callback) for a path this class serves, else None.
        kind is "connections", "connect" or "legacy"."""
        path = urlsplit(raw_path).path.rstrip("/")
        if path == "/connections":
            return "connections", None, False
        if path in LEGACY_PATHS:
            provider, callback = LEGACY_PATHS[path]
            return "legacy", provider, callback
        parts = path.split("/")
        # "", "connect", provider[, "callback"]
        if len(parts) in (3, 4) and parts[1] == "connect" and parts[2]:
            if len(parts) == 4 and parts[3] != "callback":
                return None
            return "connect", parts[2], len(parts) == 4
        return None

    def matches(self, raw_path: str) -> bool:
        return self._route(raw_path) is not None

    def handle(self, handler: Any, method: str) -> None:
        """Serve one request. `handler` is the http_server _Handler."""
        kind, provider, callback = self._route(handler.path) or ("", None, False)
        query = {k: v[0] for k, v in parse_qs(urlsplit(handler.path).query,
                                               keep_blank_values=True).items()}
        try:
            if kind == "connections":
                if method == "GET":
                    self._list(handler)
                elif method == "POST":
                    self._post(handler)
                elif method == "DELETE":
                    self._delete(handler, query)
                else:
                    raise _Refused(405, f"{method} is not allowed on /connections")
                return
            if method != "GET":
                raise _Refused(405, f"{method} is not allowed on this path")
            assert provider is not None
            legacy = kind == "legacy"
            if callback:
                self._callback(handler, provider, query, legacy=legacy)
            else:
                self._begin(handler, provider, query, legacy=legacy)
        except _Refused as exc:
            self._send(handler, exc.status, {"error": exc.message})

    # ---- helpers

    @property
    def store(self) -> Any:
        return self.service.connection_store

    @staticmethod
    def _send(handler: Any, status: int, payload: Mapping[str, Any]) -> None:
        from fabri.service.setups_routes import scrub

        handler._send_json(status, scrub(dict(payload)))

    @staticmethod
    def _redirect(handler: Any, location: str) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _auth_on(self) -> bool:
        return bool(getattr(self.service, "auth_enabled", False))

    def _caller(self, handler: Any, *, write: bool) -> Any:
        """The principal for a /connections request, or None when the server
        is open. Raises _Refused when the caller may not proceed at all."""
        principal = handler._principal()
        if principal is not None:
            return principal
        if self._auth_on():
            raise _Refused(401, "auth required")
        # Auth off: the same token rule as SetupsApi.
        tokens = ["FABRI_SERVICE_TOKEN"] + (["FABRI_ADMIN_TOKEN"] if write else [])
        if any(os.environ.get(name) for name in tokens):
            raise _Refused(401, "auth required")
        return None

    def _require_admin(self, principal: Any) -> None:
        """Org writes: an admin when auth is on, or any caller that got past
        ``_caller`` when it is off (a non-admin token is still refused)."""
        if principal is None:
            if self._auth_on():
                raise _Refused(401, "auth required")
            return
        if not principal.is_admin:
            raise _Refused(403, "admin role required")

    def _user_owner(self, principal: Any) -> str:
        """The user id that owns a user-scoped connection made by this caller."""
        if not self._auth_on():
            raise _Refused(400, "user-scoped connections need a signed-in user")
        if principal is None:
            raise _Refused(401, "auth required")
        if not principal.user_id:
            raise _Refused(400, "user-scoped connections need a signed-in user, not a token")
        return principal.user_id

    @staticmethod
    def _entry(provider: str) -> Any:
        from fabri.tools.connector_catalog import CONNECTORS

        plain = set(provider) <= _PROVIDER_CHARS and len(provider) <= 40
        entry = CONNECTORS.get(provider) if plain else None
        if entry is None:
            # Echo the name only when it looks like one, never arbitrary input.
            raise _Refused(404, f"unknown provider {provider!r}" if plain else "unknown provider")
        return entry

    @staticmethod
    def _scope(value: Any, entry: Any) -> str:
        scope = value if value not in (None, "") else getattr(entry, "scope_default", "org")
        if scope not in ("org", "user"):
            raise _Refused(400, "scope must be org or user")
        return scope

    @staticmethod
    def _read_body(handler: Any) -> dict[str, Any]:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            raise _Refused(400, "invalid Content-Length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise _Refused(413, f"body is over {MAX_BODY_BYTES} bytes")
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            # The JSON error text quotes the body, which holds the token.
            raise _Refused(400, "invalid request JSON") from None
        if not isinstance(body, dict):
            raise _Refused(400, "request body must be a JSON object")
        return body

    # ---- /connections

    def _list(self, handler: Any) -> None:
        principal = self._caller(handler, write=False)
        user_id = principal.user_id if principal is not None else None
        sees_all = not self._auth_on() or principal is None or principal.is_admin
        rows = []
        for row in self.store.list():
            mine = bool(user_id) and row.get("scope_kind") == "user" and row.get("handle") == user_id
            if not sees_all and row.get("scope_kind") == "user" and not mine:
                continue
            rows.append({**row, "mine": mine})
        self._send(handler, 200, {"connections": rows})

    def _post(self, handler: Any) -> None:
        from fabri.tools.connections import (
            DEFAULT_HANDLE, HANDLE_RULE, ConnectError, connect_static, handle_ok, store_provider)

        principal = self._caller(handler, write=True)
        body = self._read_body(handler)
        provider = body.get("provider")
        if not isinstance(provider, str) or not provider:
            raise _Refused(400, "missing required field 'provider'")
        entry = self._entry(provider)
        scope = self._scope(body.get("scope"), entry)
        if scope == "user":
            handle = self._user_owner(principal)
        else:
            self._require_admin(principal)
            handle = body.get("handle") or DEFAULT_HANDLE
            if not isinstance(handle, str) or not handle_ok(handle):
                raise _Refused(400, HANDLE_RULE)
        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise _Refused(400, "missing required field 'token'")
        verify = body.get("verify", True)
        if not isinstance(verify, bool):
            raise _Refused(400, "verify must be true or false")
        try:
            existing = self.store.get(store_provider(entry), handle)
            if existing is not None and existing.get("scope_kind") != scope:
                raise _Refused(409, f"{entry.name}:{handle} exists with another scope; remove it first")
            row = connect_static(self.store, entry, handle=handle, token=token, scope_kind=scope,
                                 transport=self.transport, verify=verify)
        except ConnectError as exc:
            raise _Refused(400, str(exc)) from None
        user_id = principal.user_id if principal is not None else None
        mine = scope == "user" and row.get("handle") == user_id
        self._send(handler, 200, {"connection": {**row, "mine": mine}})

    def _delete(self, handler: Any, query: Mapping[str, str]) -> None:
        principal = self._caller(handler, write=True)
        provider, handle = query.get("provider") or "", query.get("handle") or ""
        if not provider or not handle:
            raise _Refused(400, "provider and handle are required")
        row = self.store.get(provider, handle)
        if row is None:
            self._send(handler, 200, {"deleted": False})
            return
        if row.get("scope_kind") == "user":
            if not self._auth_on() or principal is None:
                raise _Refused(400, "user-scoped connections need a signed-in user")
            if not principal.is_admin and principal.user_id != handle:
                # Same answer as a missing row: another user's rows stay hidden.
                self._send(handler, 200, {"deleted": False})
                return
        else:
            self._require_admin(principal)
        self._send(handler, 200, {"deleted": bool(self.store.delete(provider, handle))})

    # ---- OAuth

    def _oauth_entry(self, provider: str) -> Any:
        entry = self._entry(provider)
        if getattr(entry, "oauth", None) is None:
            raise _Refused(400, f"{entry.name} connects with a token: POST /connections")
        return entry

    def _oauth_provider(self, entry: Any) -> Any:
        from fabri.tools.connections.oauth import OAuthCodeProvider

        return OAuthCodeProvider(entry, transport=self.transport)

    def _begin(self, handler: Any, provider: str, query: Mapping[str, str], *, legacy: bool) -> None:
        from fabri.service.slack_oauth import public_base_url

        entry = self._oauth_entry(provider)
        scope = "org" if legacy else self._scope(query.get("scope"), entry)
        principal = handler._principal()
        if scope == "user":
            user_id: str | None = self._user_owner(principal)
            handle: str | None = user_id
        else:
            # Auth off: open, as the older /slack/install was. A browser
            # redirect cannot carry a bearer token, so no token rule here.
            if self._auth_on():
                self._require_admin(principal)
            user_id = principal.user_id if principal is not None else None
            handle = None
        oauth = self._oauth_provider(entry)
        base = public_base_url()
        if not base or not oauth.configured():
            raise _Refused(500, f"{entry.name} OAuth is not configured")
        from fabri.tools.connections import ConnectError

        callback = entry.oauth.callback_alias or f"/connect/{entry.name}/callback"
        try:
            url = oauth.begin(self.store, redirect_uri=base + callback, scope_kind=scope,
                              handle=handle, user_id=user_id)
        except ConnectError as exc:
            raise _Refused(400, str(exc)) from None
        self._redirect(handler, url)

    def _callback(self, handler: Any, provider: str, query: Mapping[str, str], *, legacy: bool) -> None:
        entry = self._oauth_entry(provider)
        if legacy:
            done, failed = f"/?{entry.name}=connected#settings", f"/?{entry.name}=error#settings"
        else:
            done = "/?" + urlencode({"connect": entry.name, "status": "connected"}) + "#settings"
            failed = "/?" + urlencode({"connect": entry.name, "status": "error"}) + "#settings"
        state, code = query.get("state") or "", query.get("code") or ""
        if query.get("error") or not state or not code:
            # The pending row is left to expire.
            self._redirect(handler, failed)
            return
        principal = handler._principal()
        if self._auth_on() and principal is None:
            self._redirect(handler, failed)
            return
        checked = _PrincipalCheckedStore(
            self.store,
            user_id=principal.user_id if principal is not None else None,
            is_admin=bool(principal is not None and principal.is_admin),
            auth_on=self._auth_on(),
        )
        try:
            self._oauth_provider(entry).finish(checked, state=state, code=code)
        except Exception as exc:  # noqa: BLE001 -- logged by type only, never the message
            logger.warning("OAuth exchange failed for %s: %s", entry.name, type(exc).__name__)
            self._redirect(handler, failed)
            return
        self._redirect(handler, done)


__all__ = ["LEGACY_PATHS", "ConnectionsApi"]
