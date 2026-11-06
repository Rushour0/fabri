"""Make a Cakemail personal access token (PAT) from a username and password.

Two calls, both from Cakemail API 1.30.2 (``tests/fixtures/cakemail_openapi_1.30.2.json``):

1. ``POST /token`` (form, password grant, ``scopes=user``) returns a short
   lived ``access_token``.
2. ``POST /users/self/pats`` (JSON, bearer of that access token) returns the
   PAT once, in ``plaintext``.

The password and the short lived token stay local variables; neither is
stored, logged or put in an error. Error messages carry the step name and
the HTTP status only.

The PAT asks for exactly the scopes the curated Cakemail tools declare
(``security[].PersonalAccessToken`` on the 28 curated operations of the
packaged spec), so it can do what the connector exposes and nothing more.
An account behind a second factor gets a challenge instead of a token from
step 1; that case is refused, and the person makes a PAT in the Cakemail
app and passes it with ``fabri connect cakemail --token-stdin``.
"""
from __future__ import annotations

import functools
import json
import time
from collections.abc import Sequence
from importlib import resources

import httpx

from fabri.tools import connections
from fabri.tools.connections import ConnectError

CAKEMAIL_TOKEN_URL = "https://api.cakemail.dev/token"
CAKEMAIL_PATS_URL = "https://api.cakemail.dev/users/self/pats"


@functools.lru_cache(maxsize=1)
def pat_scopes() -> tuple[str, ...]:
    """The union of the PAT scopes the curated operations need, sorted."""
    from fabri.tools.connector_catalog import cakemail

    text = resources.files("fabri.tools.connector_catalog").joinpath("specs", cakemail.SPEC_FILE) \
        .read_text(encoding="utf-8")
    spec = json.loads(text)
    curated = set(cakemail.CURATED)
    scopes: set[str] = set()
    found: set[str] = set()
    for item in (spec.get("paths") or {}).values():
        if not isinstance(item, dict):
            continue
        for op in item.values():
            if not isinstance(op, dict) or op.get("operationId") not in curated:
                continue
            found.add(op["operationId"])
            for requirement in op.get("security") or ():
                if isinstance(requirement, dict):
                    scopes.update(str(s) for s in requirement.get("PersonalAccessToken") or ())
    missing = curated - found
    if missing:
        raise ConnectError(f"the packaged Cakemail spec lacks curated operations {', '.join(sorted(missing))}")
    if not scopes:
        raise ConnectError("the packaged Cakemail spec declares no PAT scopes on the curated operations")
    return tuple(sorted(scopes))


def _post(client: httpx.Client, step: str, url: str, **kwargs) -> dict:
    try:
        resp = client.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise ConnectError(f"cakemail {step} failed ({type(exc).__name__})") from None
    if not 200 <= resp.status_code < 300:
        raise ConnectError(f"cakemail {step} failed (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError:
        raise ConnectError(f"cakemail {step} returned a body that is not JSON (HTTP {resp.status_code})") from None
    if not isinstance(body, dict):
        raise ConnectError(f"cakemail {step} returned an unexpected body (HTTP {resp.status_code})")
    return body


def create_pat(username: str, password: str, *, name: str, expires_days: int = 365,
               account_ids: Sequence[int] = (), transport: httpx.BaseTransport | None = None,
               timeout_s: float = 15.0, now: float | None = None) -> str:
    """Log in with the password grant and return a new PAT's plaintext."""
    if not username or not password:
        raise ConnectError("cakemail login needs a username and a password")
    if expires_days < 1:
        raise ConnectError("--expires-days must be at least 1")
    ids = [int(a) for a in account_ids]
    from fabri.tools.security.ssrf import validate_url

    for url in (CAKEMAIL_TOKEN_URL, CAKEMAIL_PATS_URL):
        try:
            validate_url(url)
        except ValueError:
            raise ConnectError("cakemail login URL was refused") from None
    scopes = list(pat_scopes())
    when = time.time() if now is None else now
    with httpx.Client(transport=transport if transport is not None else connections._transport,
                      follow_redirects=False, timeout=timeout_s) as client:
        login = _post(client, "login", CAKEMAIL_TOKEN_URL,
                      data={"grant_type": "password", "username": username, "password": password,
                            "scopes": "user"},
                      headers={"Accept": "application/json"})
        access = login.get("access_token")
        if not isinstance(access, str) or not access:
            raise ConnectError("cakemail login returned no access token (accounts with a second factor are "
                               "not supported here; make a PAT in the Cakemail app and pass --token-stdin)")
        body: dict = {"name": name, "scopes": scopes, "expires_at": int(when + expires_days * 86400)}
        if ids:
            body["allowed_account_ids"] = ids
        created = _post(client, "create PAT", CAKEMAIL_PATS_URL, json=body,
                        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    plaintext = created.get("plaintext")
    if not isinstance(plaintext, str) or not plaintext.strip():
        raise ConnectError("cakemail create PAT returned no token")
    return plaintext.strip()


__all__ = ["CAKEMAIL_PATS_URL", "CAKEMAIL_TOKEN_URL", "create_pat", "pat_scopes"]
