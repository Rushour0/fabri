import json
import sys
from pathlib import Path


def main() -> int:
    output = Path(sys.argv[1])
    source = Path("examples/agencies/changelog-release-notes/source/release_input.json")
    if not output.is_file():
        print(json.dumps({"ok": False, "error": f"missing deliverable: {output}"}))
        return 1
    data = json.loads(source.read_text())
    text = output.read_text()
    required = [
        f"# {data['product']} {data['version']}",
        f"Released {data['release_date']}.",
        "## What’s new",
        "## Fixes",
        "## Known limitations",
        *data["features"],
        *data["fixes"],
        *data["known_limitations"],
    ]
    missing = [item for item in required if item not in text]
    print(json.dumps({"ok": not missing, "missing": missing}))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
