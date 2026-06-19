import json
import os
import sys
import urllib.request

API_KEY_ENV = "SEARCH_API_KEY"
SEARCH_URL = "https://api.tavily.com/search"


def main() -> int:
    args = json.loads(sys.stdin.read())
    query = args.get("query", "")

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(json.dumps({"error": f"{API_KEY_ENV} is not set"}))
        return 1

    payload = json.dumps({"api_key": api_key, "query": query, "max_results": 5}).encode()
    req = urllib.request.Request(SEARCH_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1

    results = [
        {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
        for r in data.get("results", [])
    ]
    print(json.dumps({"results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
