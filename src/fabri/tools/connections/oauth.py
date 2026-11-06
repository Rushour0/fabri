"""The OAuth authorization code flow for any catalog connector with ``oauth``.

``OAuthCodeProvider(entry)`` does three things:

- ``begin`` records a pending handshake in the connection store (nonce, PKCE
  verifier, redirect URI, scope kind, user id) and returns the vendor's
  authorize URL. The ``state`` in that URL is signed and carries the nonce
  and the connector name only.
- ``finish`` checks the state, takes the pending row once, trades the code
  for a token, asks the connector module's ``identity`` hook who the token
  belongs to, and stores the connection. It returns the stored row, which
  has no token columns.
- ``refresh`` trades a refresh token for a new access token; the connection
  store calls it through ``default_refreshers``.

Every request goes through ``httpx.Client(follow_redirects=False)`` after
``validate_url``, so a token endpoint that answers with a redirect is
refused rather than followed. Error messages name the provider, the step and
the HTTP status (or the exception type), never a body, a code, a verifier, a
client secret or a token.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from fabri.tools.connections import ConnectError, _client, handle_ok, store_provider
from fabri.tools.connector_catalog import (
    Connector,
    ConnectorError,
    OAuthSpec,
    connector_module,
    oauth_configured,
)

if TYPE_CHECKING:
    from fabri.service.connection_store import ConnectionStore

logger = logging.getLogger(__name__)

SCOPE_KINDS = ("org", "user")


def pkce_challenge(code_verifier: str) -> str:
    """The S256 code challenge for a PKCE verifier (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class OAuthCodeProvider:
    """The authorization code flow for one connector."""

    def __init__(self, entry: Connector, *, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 15.0) -> None:
        if entry.oauth is None:
            raise ConnectError(f"{entry.name} connects with a token, not OAuth")
        self.entry = entry
        self.spec: OAuthSpec = entry.oauth
        self.transport = transport
        self.timeout_s = timeout_s

    def __repr__(self) -> str:
        return f"OAuthCodeProvider({self.entry.name!r})"

    # -- configuration -----------------------------------------------------

    def configured(self) -> bool:
        """True when the client id (and, without PKCE, the client secret) is set."""
        return oauth_configured(self.entry)

    def _client_id(self) -> str:
        return os.environ.get(self.spec.client_id_env, "") if self.spec.client_id_env else ""

    def _client_secret(self) -> str:
        return os.environ.get(self.spec.secret_env, "") if self.spec.secret_env else ""

    def resolve_scopes(self, scopes: Sequence[str] | None) -> tuple[str, ...]:
        """The default scopes plus any asked for. A short name from
        ``scopes_optional`` maps to its full scope; anything that is neither a
        short name nor a known scope is refused."""
        chosen = list(self.spec.scopes_default)
        known = set(self.spec.scopes_default) | set(self.spec.scopes_optional.values())
        for scope in scopes or ():
            full = self.spec.scopes_optional.get(scope, scope)
            if full not in known:
                raise ConnectError(f"{self.entry.name} has no scope named {str(scope)[:60]!r}")
            if full not in chosen:
                chosen.append(full)
        return tuple(chosen)

    # -- begin -------------------------------------------------------------

    def begin(self, store: ConnectionStore, *, redirect_uri: str, scope_kind: str,
              handle: str | None, user_id: str | None, scopes: Sequence[str] | None = None,
              now: float | None = None) -> str:
        """Record a pending handshake and return the vendor's authorize URL."""
        name = self.entry.name
        if not self.configured():
            raise ConnectError(f"{name} OAuth is not configured")
        if scope_kind not in SCOPE_KINDS:
            raise ConnectError("scope must be 'org' or 'user'")
        if scope_kind == "user" and not user_id:
            raise ConnectError("user-scoped connections need a signed-in user")
        if handle is not None and not handle_ok(handle):
            raise ConnectError("the handle is not valid")
        if not redirect_uri:
            raise ConnectError(f"{name} OAuth needs a redirect URI")
        from fabri.service.oauth_state import sign_state

        chosen = self.resolve_scopes(scopes)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64) if self.spec.pkce else ""
        store.pending_put(nonce=nonce, provider=name, handle=handle, scope_kind=scope_kind,
                          user_id=user_id, code_verifier=code_verifier, scopes=" ".join(chosen),
                          redirect_uri=redirect_uri, now=now)
        state = sign_state(store.state_secret(), {"n": nonce, "p": name}, now=now)
        query: dict[str, str] = {
            "client_id": self._client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.spec.scope_separator.join(chosen),
            "state": state,
        }
        if self.spec.pkce:
            query["code_challenge"] = pkce_challenge(code_verifier)
            query["code_challenge_method"] = "S256"
        query.update(self.spec.extra_authorize)
        separator = "&" if "?" in self.spec.authorize_url else "?"
        return f"{self.spec.authorize_url}{separator}{urlencode(query)}"

    # -- HTTP --------------------------------------------------------------

    def _post_form(self, url: str, form: Mapping[str, str], step: str) -> dict[str, Any]:
        name = self.entry.name
        from fabri.tools.security.ssrf import validate_url

        try:
            validate_url(url)
        except ValueError:
            raise ConnectError(f"{name} {step} URL was refused") from None
        try:
            with _client(self.transport, self.timeout_s) as client:
                resp = client.post(url, data=dict(form), headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ConnectError(f"{name} {step} failed ({type(exc).__name__})") from None
        if not 200 <= resp.status_code < 300:
            raise ConnectError(f"{name} {step} failed (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError:
            raise ConnectError(f"{name} {step} returned a body that is not JSON") from None
        if not isinstance(body, dict):
            raise ConnectError(f"{name} {step} returned a body that is not a JSON object")
        return body

    def _get_json(self, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        """GET a JSON object; handed to the connector's ``identity`` hook."""
        name = self.entry.name
        from fabri.tools.security.ssrf import validate_url

        try:
            validate_url(url)
        except ValueError:
            raise ConnectError(f"{name} identity URL was refused") from None
        try:
            with _client(self.transport, self.timeout_s) as client:
                resp = client.get(url, headers={"Accept": "application/json", **dict(headers)})
        except httpx.HTTPError as exc:
            raise ConnectError(f"{name} identity lookup failed ({type(exc).__name__})") from None
        if not 200 <= resp.status_code < 300:
            raise ConnectError(f"{name} identity lookup failed (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError:
            raise ConnectError(f"{name} identity lookup returned a body that is not JSON") from None
        if not isinstance(body, dict):
            raise ConnectError(f"{name} identity lookup returned a body that is not a JSON object")
        return body

    def _identity(self, token_response: Mapping[str, Any]) -> tuple[str | None, str | None]:
        name = self.entry.name
        hook = getattr(connector_module(name), "identity", None)
        if hook is None:
            return None, None
        try:
            handle, display = hook(token_response, self._get_json)
        except ConnectError:
            raise
        except ConnectorError as exc:
            # Catalog modules build these messages from fixed text and a
            # filtered vendor error code, never from a token or a body.
            logger.warning("%s OAuth identity step failed: %s", name, exc)
            raise ConnectError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 -- reported by type, the message may carry data
            logger.warning("%s OAuth identity step failed: %s", name, type(exc).__name__)
            raise ConnectError(f"{name} identity lookup failed ({type(exc).__name__})") from None
        return (str(handle) if handle else None), (str(display)[:200] if display else None)

    @staticmethod
    def _scope_text(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            return " ".join(str(v) for v in value)
        return None

    # -- finish ------------------------------------------------------------

    def finish(self, store: ConnectionStore, *, state: str, code: str,
               now: float | None = None) -> dict:
        """Trade the callback's code for a token and store the connection.
        Returns the stored row (no token columns). Nothing is stored when any
        step fails."""
        name = self.entry.name
        if not isinstance(code, str) or not code:
            raise ConnectError(f"{name} callback carried no authorization code")
        from fabri.service.oauth_state import verify_state

        payload = verify_state(state, store.state_secret(), now=now)
        if payload is None or not payload.get("n"):
            raise ConnectError("state invalid or expired")
        if payload.get("p") != name:
            raise ConnectError("state was issued for another provider")
        row = store.pending_pop(payload["n"], now=now)
        if row is None:
            raise ConnectError("no pending connection for this state")
        if row["provider"] != name:
            raise ConnectError("state was issued for another provider")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": row["redirect_uri"],
            "client_id": self._client_id(),
        }
        client_secret = self._client_secret()
        if client_secret:
            form["client_secret"] = client_secret
        if self.spec.pkce:
            form["code_verifier"] = row["code_verifier"]
        tok = self._post_form(self.spec.token_url, form, "token exchange")
        access = tok.get("access_token")
        if not isinstance(access, str) or not access:
            # A vendor that reports failure in a 200 body (Slack's ok: false)
            # gets to name it through its identity hook first.
            self._identity(tok)
            raise ConnectError(f"{name} token response had no access token")
        handle, display_name = self._identity(tok)
        if row["scope_kind"] == "user":
            handle = row["user_id"]
            if not handle:
                raise ConnectError("user-scoped connections need a signed-in user")
        else:
            handle = handle or row["handle"]
            if not handle:
                raise ConnectError(f"{name} did not name the account; pass a handle")
            if not handle_ok(handle):
                raise ConnectError(f"{name} named the account with a handle that is not valid")
        refresh_token = tok.get("refresh_token")
        expires_in = tok.get("expires_in")
        try:
            expires_in = float(expires_in) if expires_in else None
        except (TypeError, ValueError):
            expires_in = None
        provider = store_provider(self.entry)
        from fabri.service.connection_store import ScopeConflict

        try:
            # keep_scope reads the stored row's scope kind in the upsert's own
            # write transaction, the same rule POST /connections applies: a
            # user flow cannot replace an org row that has the same handle,
            # nor the reverse.
            store.upsert(
                provider=provider,
                handle=handle,
                access_token=access,
                scope_kind=row["scope_kind"],
                refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
                expires_in=expires_in,
                scopes=self._scope_text(tok.get("scope")) or row["scopes"],
                display_name=display_name,
                meta={"source": "oauth", "redirect_uri": row["redirect_uri"]},
                now=now,
                keep_scope=True,
            )
        except ScopeConflict as exc:
            raise ConnectError(str(exc)) from None
        stored = store.get(provider, handle)
        if stored is None:  # pragma: no cover -- the upsert above wrote it
            raise ConnectError(f"{name} connection was not stored")
        return stored

    # -- refresh -----------------------------------------------------------

    def refresh(self, refresh_token: str) -> dict:
        """Trade a refresh token for a new token response. Raises
        RuntimeError when the vendor refuses or sends no access token; the
        message names the step and the HTTP status only."""
        name = self.entry.name
        client_id = self._client_id()
        if not client_id:
            raise RuntimeError(f"{self.spec.client_id_env or 'the client id'} is needed to refresh")
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
        client_secret = self._client_secret()
        if client_secret:
            form["client_secret"] = client_secret
        try:
            body = self._post_form(self.spec.token_url, form, "token refresh")
        except ConnectError as exc:
            raise RuntimeError(str(exc)) from None
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise RuntimeError(f"{name} refresh returned no access_token")
        return body
