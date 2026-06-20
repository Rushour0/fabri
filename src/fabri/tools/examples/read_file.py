import json
import os
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "FABRI_SANDBOX_ROOT"


def main() -> int:
    args = json.loads(sys.stdin.read())
    root_env = os.environ.get(SANDBOX_ROOT_ENV)
    if not root_env:
        print(json.dumps({"error": f"{SANDBOX_ROOT_ENV} is not set; refusing to run unsandboxed"}))
        return 1
    root = Path(root_env).resolve()
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
