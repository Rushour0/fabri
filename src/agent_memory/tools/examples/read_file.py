import json
import os
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "AGENT_SANDBOX_ROOT"


def main() -> int:
    args = json.loads(sys.stdin.read())
    root = Path(os.environ.get(SANDBOX_ROOT_ENV, ".")).resolve()
    target = (root / args["path"]).resolve()

    if not target.is_relative_to(root):
        print(json.dumps({"error": f"path escapes sandbox root: {args['path']}"}))
        return 1

    if not target.is_file():
        print(json.dumps({"error": f"no such file: {args['path']}"}))
        return 1

    print(json.dumps({"path": str(target.relative_to(root)), "content": target.read_text()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
