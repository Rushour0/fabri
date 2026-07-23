"""SSRF guards shared by tools that make model-controlled HTTP requests."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from collections.abc import Mapping

ALLOWED_SCHEMES = {"http", "https"}
ALLOW_PRIVATE_ENV = "FABRI_FETCH_ALLOW_PRIVATE"


def host_is_blocked(host: str) -> bool:
    """Return whether a host resolves to a non-public address."""
    if os.environ.get(ALLOW_PRIVATE_ENV):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def validate_url(url: str) -> str:
    """Validate an HTTP(S) URL and return it unchanged."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"refused: only http(s) supported, got {url!r}")
    if not parsed.hostname:
        raise ValueError("refused: URL has no host")
    if host_is_blocked(parsed.hostname):
        raise ValueError(
            f"refused: {parsed.hostname!r} resolves to a private/reserved address"
        )
    return url


class ValidatingRedirect(urllib.request.HTTPRedirectHandler):
    """Revalidate every redirect target before urllib follows it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


__all__ = [
    "ALLOWED_SCHEMES",
    "ALLOW_PRIVATE_ENV",
    "ValidatingRedirect",
    "host_is_blocked",
    "validate_url",
]
