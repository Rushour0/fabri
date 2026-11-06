"""Cross-site checks for the HTTP API of ``fabri serve``.

A page in any browser tab can send a request to a fabri on localhost, so a
mutation is checked before it runs:

- ``Sec-Fetch-Site: cross-site`` is refused (403).
- An ``Origin`` header that is not the service's own origin is refused
  (403). That covers ``Origin: null``, which sandboxed frames and some
  redirects send. The own origins are the origin of
  ``FABRI_PUBLIC_BASE_URL`` and, when the ``Host`` header names a loopback
  address, ``http(s)://<Host>``.
- A route that reads a JSON body needs ``Content-Type: application/json``
  (415). A cross-site HTML form cannot send that type without a CORS
  preflight, which fabri never answers.

``check_request`` returns whether the request is shown to be same-origin: a
matching ``Origin``, or ``Sec-Fetch-Site: same-origin`` (``none`` too for a
typed URL, when the caller allows navigations) with a ``Host`` that names a
loopback address or the public base URL's host. A DNS-rebound page reaches
localhost under its own host name, so it fails the Host test.

``need_same_origin`` then refuses an auth-off mutation that is neither
shown to be same-origin nor carries the service or admin token. Browsers
send these headers on their own and a page cannot forge them. A script or
CLI that drives the API with auth off sends ``Sec-Fetch-Site: same-origin``
(or an ``Origin`` of the service) or the token.

Messages here never hold a header value.
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit


class Refused(Exception):
    """A request a route refuses, with the HTTP status and a message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def is_loopback_host(host: str | None) -> bool:
    """True for localhost names and loopback IP literals."""
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` for a URL, lowercased, default port dropped."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is None or (parts.scheme, port) in (("http", 80), ("https", 443)):
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def public_base_problem(value: str) -> str | None:
    """Why a FABRI_PUBLIC_BASE_URL value cannot be used, or None. Empty is
    fine: OAuth is then simply not configured."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return "FABRI_PUBLIC_BASE_URL is not a valid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "FABRI_PUBLIC_BASE_URL must be an http or https URL, such as https://fabri.example.com"
    if parts.username or parts.password or parts.query or parts.fragment:
        return "FABRI_PUBLIC_BASE_URL must be a plain origin and path, with no user, query or fragment"
    if parts.scheme != "https" and not is_loopback_host(parts.hostname):
        return "FABRI_PUBLIC_BASE_URL must use https unless it names a loopback address"
    return None


def header(handler: Any, name: str) -> str:
    headers = getattr(handler, "headers", None) or {}
    return (headers.get(name) or "").strip()


def _usable_base() -> str:
    from fabri.service.slack_oauth import public_base_url

    base = public_base_url()
    return base if base and not public_base_problem(base) else ""


def host_trusted(handler: Any) -> bool:
    """The Host header names a loopback address or the public base URL's host."""
    try:
        hostname = urlsplit("//" + header(handler, "Host")).hostname
    except ValueError:
        return False
    if is_loopback_host(hostname):
        return True
    base = _usable_base()
    if not hostname or not base:
        return False
    return hostname == (urlsplit(base).hostname or "").lower()


def own_origins(handler: Any) -> set[str]:
    origins: set[str] = set()
    base = _usable_base()
    if base:
        origins.add(origin_of(base) or "")
    host = header(handler, "Host")
    try:
        loopback = is_loopback_host(urlsplit("//" + host).hostname)
    except ValueError:
        loopback = False
    if host and loopback:
        for scheme in ("http", "https"):
            origins.add(origin_of(f"{scheme}://{host}") or "")
    origins.discard("")
    return origins


def check_request(handler: Any, *, json_body: bool = False, navigation: bool = False) -> bool:
    """Refuse a cross-site request (raises Refused). Returns True when the
    request is shown to be same-origin, which auth-off mutations need."""
    site = header(handler, "Sec-Fetch-Site").lower()
    if site == "cross-site":
        raise Refused(403, "cross-site request refused")
    origin = header(handler, "Origin")
    if origin and origin_of(origin) not in own_origins(handler):
        raise Refused(403, "request from another origin refused")
    if json_body:
        ctype = header(handler, "Content-Type").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Refused(415, "Content-Type must be application/json")
    if origin:
        return True
    if not host_trusted(handler):
        return False
    return site == "same-origin" or (navigation and site == "none")


def need_same_origin(*, auth_on: bool, principal: Any, same_origin: bool, what: str = "changes") -> None:
    """With auth off, a mutation needs a same-origin request or a token."""
    if auth_on or principal is not None or same_origin:
        return
    raise Refused(403, f"with auth off, {what} need a same-origin request (Studio) or the service "
                       "or admin token")


__all__ = ["Refused", "check_request", "header", "host_trusted", "is_loopback_host", "need_same_origin",
           "origin_of", "own_origins", "public_base_problem"]
