"""Recipe: recursive regex grep over a directory, capped output."""
import json
import re
import sys
from pathlib import Path


def main() -> int:
    args = json.loads(sys.stdin.read())
    base = Path(args["path"])
    if not base.is_dir():
        print(json.dumps({"error": f"not a directory: {base}"}))
        return 1
    try:
        pat = re.compile(args["pattern"])
    except re.error as e:
        print(json.dumps({"error": f"bad pattern: {e}"}))
        return 1
    glob = args.get("glob") or "*"
    max_matches = int(args.get("max_matches", 200))

    hits = []
    truncated = False
    for p in base.rglob(glob):
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pat.search(line):
                hits.append({"file": str(p.relative_to(base)), "line": i, "text": line[:300]})
                if len(hits) >= max_matches:
                    truncated = True
                    break
        if truncated:
            break

    print(json.dumps({
        "path": str(base), "pattern": args["pattern"], "glob": glob,
        "matches": hits, "truncated": truncated,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
