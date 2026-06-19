import json
from dataclasses import dataclass
from pathlib import Path


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
        # command parts that name a file sitting next to the manifest (e.g. the
        # tool's own script) are resolved to absolute paths, so manifests stay
        # portable regardless of the caller's cwd. Bare executables (python3, go
        # ...) are left untouched.
        command = [
            str((manifest_dir / part).resolve()) if (manifest_dir / part).is_file() else part
            for part in data["command"]
        ]
        return cls(
            name=data["name"],
            description=data["description"],
            command=command,
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            timeout_s=data.get("timeout_s", 10.0),
        )
