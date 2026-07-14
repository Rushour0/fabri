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


def main(output_path: str) -> int:
    failures = check(args["source_path"], output_path)
    print(json.dumps({"ok": not failures, "failures": failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    args = json.loads(sys.stdin.read())
    try:
        raise SystemExit(main(args["output_path"]))
    except SystemExit:
        raise
    except Exception as exc:
        # Report the sandbox-relative output_path, not str(exc): a
        # FileNotFoundError's message embeds the resolved absolute host path,
        # which would otherwise leak the workspace/user directory layout into
        # tool output an LLM sees.
        print(json.dumps({
            "ok": False,
            "failures": [f"{type(exc).__name__} while checking {args['output_path']}"],
        }))
        raise SystemExit(1)
