"""now -- example fabri tool bundled in the 'clock' skill.

Reads one JSON object from stdin (no args required) and prints the current UTC
time as one JSON object on stdout: {"utc": "<ISO-8601 timestamp>"}."""
import json
import sys
from datetime import datetime, timezone


def main() -> int:
    json.loads(sys.stdin.read() or "{}")  # drain stdin; the tool takes no args
    print(json.dumps({"utc": datetime.now(timezone.utc).isoformat()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
