"""GitHub App helpers for the multi-tenant install flow.

`install_url()` is the public "install this App" link used by the Studio
Connect button; `verify_webhook_signature()` authenticates inbound
`/github/webhook` deliveries by HMAC over the raw request body.
"""
import hashlib
import hmac
import os


def install_url() -> str | None:
    """The GitHub App install URL, or None when the slug is unconfigured."""
    slug = os.environ.get("GITHUB_APP_SLUG", "")
    if not slug:
        return None
    return f"https://github.com/apps/{slug}/installations/new"


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
