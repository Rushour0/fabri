"""HTTP GET fetcher with HTML stripping and an 8KB output cap.

SSRF-hardened: only http(s), refuses hosts resolving to private/reserved
addresses, and validates redirect hops.
"""
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = {"http", "https"}
ALLOW_PRIVATE_ENV = "FABRI_FETCH_ALLOW_PRIVATE"


def strip_html(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _host_is_blocked(host: str) -> bool:
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
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def _validate(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"refused: only http(s) supported, got {url!r}")
    if not parsed.hostname:
        raise ValueError("refused: URL has no host")
    if _host_is_blocked(parsed.hostname):
        raise ValueError(f"refused: {parsed.hostname!r} resolves to a private/reserved address")
    return url


class _ValidatingRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_ValidatingRedirect)


def main() -> int:
    args = json.loads(sys.stdin.read())
    url = args["url"]
    try:
        url = _validate(url)
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    request = urllib.request.Request(url, headers={"User-Agent": "fabri-recipe/0.1"})
    try:
        with _opener.open(request, timeout=20) as response:
            body = response.read(64 * 1024).decode("utf-8", errors="replace")
            status = response.status
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps({"url": url, "status": status, "text": strip_html(body)[:8000]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
