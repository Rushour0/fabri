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


def section(title: str, items: list[str]) -> list[str]:
    return [f"## {title}", "", *[f"- {item}" for item in items], ""]


def main(source_path: str, output_path: str) -> int:
    source = sandbox_path(source_path)
    output = sandbox_path(output_path)
    data = json.loads(source.read_text())
    required = ("product", "version", "release_date", "features", "fixes", "known_limitations")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"release input missing: {', '.join(missing)}")
    lines = [
        f"# {data['product']} {data['version']}",
        "",
        f"Released {data['release_date']}.",
        "",
    ]
    lines.extend(section("What’s new", data["features"]))
    lines.extend(section("Fixes", data["fixes"]))
    lines.extend(section("Known limitations", data["known_limitations"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
    print(json.dumps({"output_path": str(output.relative_to(Path.cwd())), "sections": 3}))
    return 0


if __name__ == "__main__":
    args = json.loads(sys.stdin.read())
    try:
        raise SystemExit(main(args["source_path"], args["output_path"]))
    except SystemExit:
        raise
    except Exception as exc:
        # Report the sandbox-relative output_path, not str(exc): a
        # FileNotFoundError's message embeds the resolved absolute host path,
        # which would otherwise leak the workspace/user directory layout into
        # tool output an LLM sees.
        print(json.dumps({"error": f"{type(exc).__name__} while building {args['output_path']}"}))
        raise SystemExit(1)
