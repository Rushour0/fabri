"""Signed OAuth ``state`` values for the generic connect flow.

A state is ``<payload>.<expiry>.<signature>``: the payload is a JSON object
of short strings encoded as URL-safe base64 without padding, the expiry is a
unix time in whole seconds, and the signature is a hex HMAC-SHA256 over
``<payload>.<expiry>`` with the server's state secret.

The payload carries a nonce and a provider name only. The PKCE verifier,
the redirect URI and the user id stay server side in the ``pending_oauth``
table, keyed by the nonce, so nothing a browser sees can be replayed into a
token. ``fabri.service.slack_oauth`` keeps its own older helpers for the
Slack signing-secret flow; this module does not replace them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping

DEFAULT_TTL_S = 600


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def sign_state(
    secret: str,
    payload: Mapping[str, str],
    *,
    ttl_s: int = DEFAULT_TTL_S,
    now: float | None = None,
) -> str:
    """A state string carrying `payload`, valid for `ttl_s` seconds."""
    if not secret:
        raise ValueError("the OAuth state secret must not be empty")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in payload.items()):
        raise ValueError("the OAuth state payload must map strings to strings")
    now = time.time() if now is None else now
    body = _b64encode(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode())
    expiry = int(now) + int(ttl_s)
    message = f"{body}.{expiry}"
    return f"{message}.{_sign(secret, message)}"


def verify_state(state: str, secret: str, *, now: float | None = None) -> dict | None:
    """The payload of a state this secret signed, or None when the state is
    malformed, tampered with or expired."""
    if not secret or not isinstance(state, str):
        return None
    parts = state.split(".")
    if len(parts) != 3:
        return None
    body, expiry_text, signature = parts
    if not hmac.compare_digest(signature, _sign(secret, f"{body}.{expiry_text}")):
        return None
    try:
        expiry = int(expiry_text)
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError):
        return None
    now = time.time() if now is None else now
    if expiry < now:
        return None
    if not isinstance(payload, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in payload.items()
    ):
        return None
    return payload
