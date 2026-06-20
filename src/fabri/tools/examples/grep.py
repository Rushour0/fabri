"""Regex search across sandboxed files. Pure-Python so it doesn't depend on
ripgrep being installed; output is capped so a runaway match doesn't blow
the agent's context."""
import json
import os
import re
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "FABRI_SANDBOX_ROOT"
MAX_MATCHES = 200


def main() -> int:
    args = json.loads(sys.stdin.read())
    root_env = os.environ.get(SANDBOX_ROOT_ENV)
    if not root_env:
        print(json.dumps({"error": f"{SANDBOX_ROOT_ENV} is not set; refusing to run unsandboxed"}))
        return 1
    root = Path(root_env).resolve()
    search_root = (root / args.get("path", ".")).resolve()

    if not search_root.is_relative_to(root):
        print(json.dumps({"error": f"path escapes sandbox root: {args.get('path')}"}))
        return 1
    if not search_root.exists():
        print(json.dumps({"error": f"no such path: {args.get('path')}"}))
        return 1

    try:
        pattern = re.compile(args["pattern"])
    except re.error as e:
        print(json.dumps({"error": f"invalid regex: {e}"}))
        return 1

    glob = args.get("glob", "**/*")
    paths = [search_root] if search_root.is_file() else sorted(search_root.glob(glob))

    matches = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                matches.append({
                    "path": str(p.relative_to(root)), "line": line_no, "text": line[:500],
                })
                if len(matches) >= MAX_MATCHES:
                    break
        if len(matches) >= MAX_MATCHES:
            break

    print(json.dumps({"matches": matches, "truncated": len(matches) >= MAX_MATCHES}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
