"""Fetch a URL and return its text body. No API key; just network egress.
Pairs with web_search: search returns links, fetch reads them.

SSRF hardening: the URL is model-controlled, so without guards the agent could
be steered (e.g. by a prompt-injected page or task) into reading cloud metadata
(169.254.169.254 -> IAM credentials), internal/RFC1918 services, or local files
via file://. We (1) allow only http(s), (2) resolve the host and refuse any
private/loopback/link-local/reserved address, and (3) re-validate every redirect
hop so a public URL can't 302 to the metadata IP. A residual DNS-rebind TOCTOU
remains (the host re-resolves between check and connect); the practical
metadata / internal-service / file:// attacks are closed.
"""
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 200_000  # cap response so the agent can't blow its context on one page
DEFAULT_TIMEOUT = 10
ALLOWED_SCHEMES = {"http", "https"}
# Escape hatch for users who genuinely need to fetch a localhost/internal dev
# service. Off by default (secure); `file://` is refused either way.
ALLOW_PRIVATE_ENV = "FABRI_FETCH_ALLOW_PRIVATE"


def _host_is_blocked(host: str) -> bool:
    """True if the host doesn't resolve, or ANY of its addresses falls in a
    non-public range. Resolving (not string-matching) defeats DNS names and
    decimal/hex/IPv6 encodings that point at internal IPs."""
    if os.environ.get(ALLOW_PRIVATE_ENV):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # unresolvable -> refuse rather than let urllib try
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped  # unwrap ::ffff:127.0.0.1 etc.
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def _validate(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"refused: only http(s) allowed, got scheme {p.scheme!r}")
    if not p.hostname:
        raise ValueError("refused: URL has no host")
    if _host_is_blocked(p.hostname):
        raise ValueError(f"refused: {p.hostname!r} resolves to a private/reserved address")
    return url


class _ValidatingRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate each redirect target. A pure pre-flight check is bypassable
    by a public URL that 302s to the metadata IP; validating every hop closes
    that leg (the validator raises, aborting the redirect)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_ValidatingRedirect)


def main() -> int:
    args = json.loads(sys.stdin.read())
    url = args["url"]
    timeout = args.get("timeout", DEFAULT_TIMEOUT)

    try:
        url = _validate(url)
    except ValueError as e:
        print(json.dumps({"error": str(e), "url": url}))
        return 1

    req = urllib.request.Request(url, headers={"User-Agent": "fabri/0.1"})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES + 1)
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": f"HTTP {e.code}: {e.reason}", "url": url}))
        return 1
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
        # ValueError surfaces a refused redirect hop from _ValidatingRedirect.
        print(json.dumps({"error": str(e), "url": url}))
        return 1

    truncated = len(body) > MAX_BYTES
    text = body[:MAX_BYTES].decode("utf-8", errors="replace")
    print(json.dumps({
        "url": url, "status": status, "content_type": content_type,
        "truncated": truncated, "body": text,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
