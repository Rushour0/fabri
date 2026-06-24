"""Recipe: an HTTP GET fetcher with HTML stripping + an 8KB output cap.

Drop next to its manifest in your tools/agent_tools/ directory.

SSRF-hardened: only http(s), refuses hosts that resolve to private/reserved
addresses (cloud metadata, localhost, RFC1918), and re-validates redirect hops
so a public URL can't 302 to an internal IP. The model controls the URL, so a
bare fetcher is an internal-network / metadata-credential read primitive."""
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
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"refused: only http(s) supported, got {url!r}")
    if not p.hostname:
        raise ValueError("refused: URL has no host")
    if _host_is_blocked(p.hostname):
        raise ValueError(f"refused: {p.hostname!r} resolves to a private/reserved address")
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
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    req = urllib.request.Request(url, headers={"User-Agent": "fabri-recipe/0.1"})
    try:
        with _opener.open(req, timeout=20) as resp:
            body = resp.read(64 * 1024).decode("utf-8", errors="replace")
            status = resp.status
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps({"url": url, "status": status, "text": strip_html(body)[:8000]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
