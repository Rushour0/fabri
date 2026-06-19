"""Surgical string-replace edit. Forces unique-match unless replace_all=true so
the agent can't accidentally clobber unrelated lines. Mirrors the sandbox
path-jail used by read_file/write_file."""
import json
import os
import sys
from pathlib import Path

SANDBOX_ROOT_ENV = "AGENT_SANDBOX_ROOT"


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

    old, new = args["old"], args["new"]
    replace_all = bool(args.get("replace_all", False))
    content = target.read_text()

    count = content.count(old)
    if count == 0:
        print(json.dumps({"error": "old string not found"}))
        return 1
    if count > 1 and not replace_all:
        print(json.dumps({"error": f"old string is not unique ({count} matches); pass replace_all=true to replace every occurrence"}))
        return 1

    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    target.write_text(updated)
    print(json.dumps({"path": str(target.relative_to(root)), "replacements": count if replace_all else 1}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
