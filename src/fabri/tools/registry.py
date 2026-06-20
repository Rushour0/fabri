from pathlib import Path

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.runner import run_tool


class ToolRegistry:
    def __init__(self, manifest_dir: Path | list[Path]):
        # A project typically wants the framework's generic tools (read_file,
        # web_search, ...) plus its own domain tools discovered from the same
        # registry -- accepting a list lets a config list multiple directories
        # instead of forcing everything into one folder.
        dirs = [manifest_dir] if isinstance(manifest_dir, Path) else list(manifest_dir)
        self.manifest_dirs = dirs
        self.tools: dict[str, ToolManifest] = {}
        for manifest_dir in dirs:
            for path in sorted(manifest_dir.glob("*.json")):
                manifest = ToolManifest.from_file(path)
                self.tools[manifest.name] = manifest

    def register(self, manifest: ToolManifest) -> None:
        """Add a manifest built programmatically rather than discovered from a
        manifest_dir -- used for agent-as-tool manifests (see tools/agent_tool.py),
        which are generated per-config rather than read from a JSON file."""
        self.tools[manifest.name] = manifest

    def list(self) -> list[ToolManifest]:
        return list(self.tools.values())

    def invoke(self, name: str, args: dict) -> dict:
        manifest = self.tools.get(name)
        if manifest is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        return run_tool(manifest, args)
