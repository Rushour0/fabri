"""SSRF guards shared by tools that make model-controlled HTTP requests."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping

# The fetch_url recipes import this module as plain scripts that need only
# the standard library, so the pinned transport below exists only when httpx
# is installed.
try:
    import httpcore
    import httpx
except ImportError:  # pragma: no cover -- exercised by stdlib-only callers
    httpcore = httpx = None  # type: ignore[assignment]

ALLOWED_SCHEMES = {"http", "https"}
ALLOW_PRIVATE_ENV = "FABRI_FETCH_ALLOW_PRIVATE"


def address_is_blocked(address: str) -> bool:
    """Return whether one IP address text is not a public address. A text
    that is not an IP address counts as blocked."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_is_blocked(host: str) -> bool:
    """Return whether a host resolves to a non-public address."""
    if os.environ.get(ALLOW_PRIVATE_ENV):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    return any(address_is_blocked(info[4][0]) for info in infos)


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


Resolver = Callable[[str, int], list[str]]


def _system_resolver(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for `host`, in its order.
    ``socket.getaddrinfo`` is looked up on each call so a test can swap it."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def resolve_public(host: str, port: int, resolver: Resolver | None = None) -> list[str]:
    """Resolve `host` once and return its addresses, refusing the lot when
    any one of them is not public. Raises ValueError with a "refused:" text.
    With FABRI_FETCH_ALLOW_PRIVATE set the addresses are returned unchecked,
    the same opt-in the other guards honour."""
    try:
        addresses = (resolver or _system_resolver)(host, port)
    except (OSError, UnicodeError):
        raise ValueError(f"refused: {host!r} cannot be resolved") from None
    if not addresses:
        raise ValueError(f"refused: {host!r} cannot be resolved")
    if not os.environ.get(ALLOW_PRIVATE_ENV) and any(address_is_blocked(a) for a in addresses):
        raise ValueError(f"refused: {host!r} resolves to a private/reserved address")
    return addresses


if httpx is not None:

    class PinnedNetworkBackend(httpcore.NetworkBackend):
        """An httpcore network backend that resolves each host once, checks
        every address it got, and opens the TCP connection to one of those
        checked addresses.

        Only the TCP connect sees the address. httpcore still starts TLS with
        the URL's host name as SNI and as the name the certificate must match,
        and httpx still writes the URL's host into the Host header, so pinning
        changes where the socket goes and nothing the server sees."""

        def __init__(self, resolver: Resolver | None = None,
                     inner: httpcore.NetworkBackend | None = None) -> None:
            self._resolver = resolver
            self._inner = inner or httpcore.SyncBackend()

        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.NetworkStream:
            try:
                addresses = resolve_public(host, port, self._resolver)
            except ValueError as exc:
                raise httpcore.ConnectError(str(exc)) from None
            last: Exception | None = None
            for address in addresses:
                try:
                    return self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                                   socket_options=socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    last = exc
            assert last is not None
            raise last

        def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.NetworkStream:
            raise httpcore.ConnectError("refused: Unix sockets are not reachable through the pinned transport")

        def sleep(self, seconds: float) -> None:
            self._inner.sleep(seconds)

    class PinnedTransport(httpx.HTTPTransport):
        """An httpx transport whose connections go only to addresses checked by
        :func:`resolve_public` in the same step that connects, so a DNS answer
        that changes between a check and the send cannot reach a private
        address. No proxy and no Unix socket: either would connect somewhere
        other than the checked address. Give the client ``trust_env=False`` too,
        or httpx mounts its own proxy transports from the environment."""

        def __init__(
            self,
            *,
            resolver: Resolver | None = None,
            inner_backend: httpcore.NetworkBackend | None = None,
            verify: bool | str = True,
            cert: object = None,
            trust_env: bool = True,
            http1: bool = True,
            http2: bool = False,
            limits: httpx.Limits = httpx.Limits(max_connections=100, max_keepalive_connections=20),
            retries: int = 0,
        ) -> None:
            self.network_backend = PinnedNetworkBackend(resolver, inner_backend)
            self._pool = httpcore.ConnectionPool(
                ssl_context=httpx.create_ssl_context(verify=verify, cert=cert, trust_env=trust_env),
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
                retries=retries,
                network_backend=self.network_backend,
            )


__all__ = [
    "ALLOWED_SCHEMES",
    "ALLOW_PRIVATE_ENV",
    "ValidatingRedirect",
    "address_is_blocked",
    "host_is_blocked",
    "resolve_public",
    "validate_url",
]
if httpx is not None:
    __all__ += ["PinnedNetworkBackend", "PinnedTransport"]
