"""Recipe: an HTTP GET fetcher with HTML stripping + an 8KB output cap.

Drop next to its manifest in your tools/agent_tools/ directory.

SSRF-hardened: only http(s), refuses hosts that resolve to private/reserved
addresses (cloud metadata, localhost, RFC1918), and re-validates redirect hops
so a public URL can't 302 to an internal IP. The model controls the URL, so a
bare fetcher is an internal-network / metadata-credential read primitive."""
import json
import re
import sys
import urllib.request
from pathlib import Path

# This recipe is launched as a plain script by its JSON manifest. In a source
# checkout, add the src/ root so that direct execution can import the shared
# guard without relying on an editable install or the caller's working
# directory.
if not __package__:
    for _candidate in Path(__file__).resolve().parents:
        if (_candidate / "fabri" / "tools" / "security" / "ssrf.py").is_file():
            sys.path.insert(0, str(_candidate))
            break

from fabri.tools.security.ssrf import (
    ALLOWED_SCHEMES as _ALLOWED_SCHEMES,
    ALLOW_PRIVATE_ENV as _ALLOW_PRIVATE_ENV,
    ValidatingRedirect,
    host_is_blocked,
    validate_url,
)

ALLOWED_SCHEMES = _ALLOWED_SCHEMES
ALLOW_PRIVATE_ENV = _ALLOW_PRIVATE_ENV


def strip_html(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _host_is_blocked(host: str) -> bool:
    return host_is_blocked(host)


def _validate(url: str) -> str:
    return validate_url(url)


_ValidatingRedirect = ValidatingRedirect


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
