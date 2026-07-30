import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request


SLACK_BOT_SCOPES = "chat:write,channels:read,app_mentions:read,channels:history"
SLACK_AUTHORIZE = "https://slack.com/oauth/v2/authorize"
SLACK_ACCESS = "https://slack.com/api/oauth.v2.access"
_STATE_TTL_S = 600


def public_base_url() -> str:
    return os.environ.get("FABRI_PUBLIC_BASE_URL", "").rstrip("/")


def sign_state(secret: str) -> str:
    if not secret:
        raise ValueError("SLACK_SIGNING_SECRET must not be empty")

    nonce = secrets.token_urlsafe(32)
    expiry = int(time.time()) + _STATE_TTL_S
    payload = f"{nonce}.{expiry}"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_state(state: str, secret: str) -> bool:
    if not secret:
        return False
    try:
        nonce, expiry, sig = state.split(".")
        payload = f"{nonce}.{expiry}"
        expected_sig = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected_sig) and int(expiry) >= time.time()
    except (AttributeError, TypeError, ValueError):
        return False


def build_install_redirect() -> str | None:
    client_id = os.environ.get("SLACK_CLIENT_ID", "")
    base = public_base_url()
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not client_id or not base or not secret:
        return None

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": SLACK_BOT_SCOPES,
            "redirect_uri": f"{base}/slack/oauth/callback",
            "state": sign_state(secret),
        }
    )
    return f"{SLACK_AUTHORIZE}?{query}"


def exchange_code(code: str) -> dict:
    base = public_base_url()
    data = {
        "client_id": os.environ.get("SLACK_CLIENT_ID", ""),
        "client_secret": os.environ.get("SLACK_CLIENT_SECRET", ""),
        "code": code,
        "redirect_uri": f"{base}/slack/oauth/callback",
    }
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(
        SLACK_ACCESS,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())
