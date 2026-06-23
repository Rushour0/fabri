import json
import re
from dataclasses import dataclass
from pathlib import Path

# Tokens we'll rewrite to absolute paths if they match a sibling file.
# A token is path-shaped if it has a script-like extension OR contains a path
# separator (e.g. `example_go_tool/sum_tool`). This keeps bare data words --
# `bash -c "ls grep.py"` or a positional arg that happens to share a name with
# a sibling file -- from being rewritten.
_SCRIPT_EXT_RE = re.compile(r"\.(py|js|ts|mjs|cjs|go|rs|sh|rb|exe|jar)$")


def _is_path_shaped(part: str) -> bool:
    if part.startswith("-"):  # CLI flag, not a path
        return False
    return "/" in part or bool(_SCRIPT_EXT_RE.search(part))


@dataclass
class ToolManifest:
    name: str
    description: str
    command: list[str]
    input_schema: dict
    output_schema: dict
    timeout_s: float = 10.0

    @classmethod
    def from_file(cls, path: Path) -> "ToolManifest":
        data = json.loads(path.read_text())
        manifest_dir = path.parent
        # Rewrite ONLY script-shaped tokens (`foo.py`, `./bin/run.sh`, ...) that
        # also resolve to a sibling file, so a manifest stays portable regardless
        # of the caller's cwd. Bare executables (python3, go) and arbitrary
        # string data args (e.g. `bash -c "ls grep.py"`) are left untouched.
        def _rewrite(part: str) -> str:
            if not _is_path_shaped(part):
                return part
            candidate = manifest_dir / part
            return str(candidate.resolve()) if candidate.is_file() else part

        command = [_rewrite(part) for part in data["command"]]
        return cls(
            name=data["name"],
            description=data["description"],
            command=command,
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            timeout_s=data.get("timeout_s", 10.0),
        )
