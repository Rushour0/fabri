"""Fetch a URL and return its text body. No API key; just network egress.
Pairs with web_search: search returns links, fetch reads them."""
import json
import sys
import urllib.error
import urllib.request

MAX_BYTES = 200_000  # cap response so the agent can't blow its context on one page
DEFAULT_TIMEOUT = 10


def main() -> int:
    args = json.loads(sys.stdin.read())
    url = args["url"]
    timeout = args.get("timeout", DEFAULT_TIMEOUT)

    req = urllib.request.Request(url, headers={"User-Agent": "fabri/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES + 1)
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": f"HTTP {e.code}: {e.reason}", "url": url}))
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as e:
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
