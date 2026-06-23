"""Recipe: an HTTP GET fetcher with HTML stripping + an 8KB output cap.

Drop next to its manifest in your tools/agent_tools/ directory."""
import json
import re
import sys
import urllib.request


def strip_html(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def main() -> int:
    args = json.loads(sys.stdin.read())
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        print(json.dumps({"error": f"refused: only http(s) supported, got {url!r}"}))
        return 1
    req = urllib.request.Request(url, headers={"User-Agent": "fabri-recipe/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(64 * 1024).decode("utf-8", errors="replace")
            status = resp.status
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps({"url": url, "status": status, "text": strip_html(body)[:8000]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
