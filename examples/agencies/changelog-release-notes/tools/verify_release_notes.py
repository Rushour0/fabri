import json
import os
import sys
from pathlib import Path


def sandbox_path(value: str) -> Path:
    root = Path(os.environ.get("FABRI_SANDBOX_ROOT", ".")).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes FABRI_SANDBOX_ROOT")
    return path


def check(source_path: str, output_path: str) -> list[str]:
    source = sandbox_path(source_path)
    output = sandbox_path(output_path)
    data = json.loads(source.read_text())
    if not output.is_file():
        return [f"missing deliverable: {output_path}"]
    text = output.read_text()
    failures = []
    for heading in ("## What’s new", "## Fixes", "## Known limitations"):
        if heading not in text:
            failures.append(f"missing heading: {heading}")
    expected = [
        f"# {data['product']} {data['version']}",
        f"Released {data['release_date']}.",
        *data["features"],
        *data["fixes"],
        *data["known_limitations"],
    ]
    for value in expected:
        if value not in text:
            failures.append(f"missing source item: {value}")
    return failures


def main() -> int:
    args = json.loads(sys.stdin.read())
    failures = check(args["source_path"], args["output_path"])
    print(json.dumps({"ok": not failures, "failures": failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "failures": [str(exc)]}))
        raise SystemExit(1)
