"""GitHub App helpers for the multi-tenant install flow.

`install_url()` is the public "install this App" link used by the Studio
Connect button; `verify_webhook_signature()` authenticates inbound
`/github/webhook` deliveries by HMAC over the raw request body.
"""
import hashlib
import hmac
import os
from urllib.parse import urlencode

STATE_PROVIDER = "github_app"


def install_url(state: str | None = None) -> str | None:
    """The GitHub App install URL, or None when the slug is unconfigured.

    GitHub hands a ``state`` query value on this URL back to the App's setup
    redirect unchanged, which is how ``/github/setup`` knows the install
    started from this server."""
    slug = os.environ.get("GITHUB_APP_SLUG", "")
    if not slug:
        return None
    url = f"https://github.com/apps/{slug}/installations/new"
    if state:
        url += "?" + urlencode({"state": state})
    return url


def begin_install(store, *, now: float | None = None) -> str:
    """Record one pending GitHub install in the connection store and return
    its signed, single-use state (good for 10 minutes)."""
    import secrets

    from fabri.service.oauth_state import sign_state

    nonce = secrets.token_urlsafe(32)
    store.pending_put(nonce=nonce, provider=STATE_PROVIDER, handle=None, scope_kind="org",
                      user_id=None, code_verifier="", scopes="", redirect_uri="", now=now)
    return sign_state(store.state_secret(), {"n": nonce, "p": STATE_PROVIDER}, now=now)


def consume_install_state(store, state: str, *, now: float | None = None) -> bool:
    """True once for a state that begin_install issued, that is unexpired and
    unused. Anything else, including a state for another provider, is False
    and changes nothing."""
    from fabri.service.oauth_state import verify_state

    payload = verify_state(state, store.state_secret(), now=now)
    if payload is None or payload.get("p") != STATE_PROVIDER or not payload.get("n"):
        return False
    row = store.pending_pop(payload["n"], now=now)
    return row is not None and row["provider"] == STATE_PROVIDER


def verify_webhook_signature(
    secret: str, raw_body: bytes, signature_header: str
) -> bool:
    """Verify GitHub's X-Hub-Signature-256 over the raw body. Fails closed."""
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
