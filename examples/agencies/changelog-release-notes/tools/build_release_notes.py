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


def main() -> int:
    args = json.loads(sys.stdin.read())
    source = sandbox_path(args["source_path"])
    output = sandbox_path(args["output_path"])
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
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1)
