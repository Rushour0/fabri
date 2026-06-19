import json
import os
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "AGENT_SANDBOX_ROOT"


def main() -> int:
    args = json.loads(sys.stdin.read())
    root = Path(os.environ.get(SANDBOX_ROOT_ENV, ".")).resolve()
    target = (root / args.get("path", ".")).resolve()

    if not target.is_relative_to(root):
        print(json.dumps({"error": f"path escapes sandbox root: {args.get('path')}"}))
        return 1

    if not target.is_dir():
        print(json.dumps({"error": f"no such directory: {args.get('path')}"}))
        return 1

    entries = sorted(
        ({"name": p.name, "is_dir": p.is_dir()} for p in target.iterdir()),
        key=lambda e: e["name"],
    )
    print(json.dumps({"path": str(target.relative_to(root)), "entries": entries}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
