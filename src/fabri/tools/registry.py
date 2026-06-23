from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_error, tool_ok

if TYPE_CHECKING:
    from fabri.sandbox import Sandbox

ToolHandler = Callable[[dict], dict]

BATCH_TOOL_NAME = "batch"
# Nested batches multiply fan-out unpredictably; spawn_subagent + ask_user
# have side effects that don't belong inside an opaque batch result.
BATCH_FORBIDDEN_NESTED = frozenset({BATCH_TOOL_NAME, "spawn_subagent", "ask_user"})


class ToolRegistry:
    def __init__(
        self,
        manifest_dir: Path | list[Path],
        sandbox_root: str | None = None,
        sandbox: "Sandbox | None" = None,
    ):
        dirs = [manifest_dir] if isinstance(manifest_dir, Path) else list(manifest_dir)
        self.manifest_dirs = dirs
        # Threaded into the subprocess env= per invocation rather than
        # mutating os.environ, so two concurrent registries don't clobber
        # each other's root.
        self.sandbox_root = sandbox_root
        # Lazy import: fabri.sandbox imports from fabri.tools, so a
        # top-level import here would cycle.
        if sandbox is None:
            from fabri.sandbox import LocalSandbox

            sandbox = LocalSandbox()
        self.sandbox = sandbox
        self.tools: dict[str, ToolManifest] = {}
        self._callables: dict[str, ToolHandler] = {}
        # Hold references to long-lived backing objects (e.g. MCPStdioClient
        # subprocesses) so they aren't GC'd while their handlers are live.
        self._owned_resources: list[object] = []
        for manifest_dir in dirs:
            for path in sorted(manifest_dir.glob("*.json")):
                manifest = ToolManifest.from_file(path)
                self.tools[manifest.name] = manifest

    def register(self, manifest: ToolManifest) -> None:
        """Add a manifest built programmatically rather than discovered from
        a manifest_dir (e.g. agent-as-tool manifests)."""
        self.tools[manifest.name] = manifest

    def register_callable(
        self,
        manifest: ToolManifest,
        handler: ToolHandler,
        owns: object | None = None,
    ) -> None:
        """Register a tool whose invocation is an in-process callable instead
        of a subprocess. `handler(args)` must return a tool-result dict
        (`{ok, result?, error?}` — use tool_ok/tool_error helpers).

        `owns`, if given, is held on the registry so the handler's backing
        resource isn't GC'd while it's registered.
        """
        self.tools[manifest.name] = manifest
        self._callables[manifest.name] = handler
        if owns is not None:
            self._owned_resources.append(owns)

    def list(self) -> list[ToolManifest]:
        return list(self.tools.values())

    def invoke(self, name: str, args: dict) -> dict:
        if name == BATCH_TOOL_NAME and BATCH_TOOL_NAME in self.tools:
            return self.invoke_batch(args.get("calls") or [])
        manifest = self.tools.get(name)
        if manifest is None:
            return tool_error(f"unknown tool: {name}")
        if name in self._callables:
            try:
                return self._callables[name](args)
            except Exception as e:
                return tool_error(f"{name}: handler raised {type(e).__name__}: {e}")
        extra_env = {"FABRI_SANDBOX_ROOT": self.sandbox_root} if self.sandbox_root else None
        return self.sandbox.run_tool(manifest, args, extra_env=extra_env)

    def invoke_batch(self, calls: list[dict]) -> dict:
        """Dispatch a list of `{name, args}` calls inside one tool invocation.
        Returns `{"ok": True, "result": {"results": [...]}}` where each entry
        is the standard `{ok, result?, error?}` shape — a per-call failure
        does NOT short-circuit the batch (the model gets every result and
        decides what to do). Nested batches and side-effecting meta-tools
        are refused with a clear error so the model retries with the
        flattened calls instead."""
        if not isinstance(calls, list):
            return tool_error("batch: `calls` must be a list of {name, args} objects")
        results: list[dict] = []
        for entry in calls:
            if not isinstance(entry, dict) or "name" not in entry:
                results.append(tool_error("batch entry malformed: expected {name, args}"))
                continue
            inner_name = entry["name"]
            if inner_name in BATCH_FORBIDDEN_NESTED:
                results.append(tool_error(
                    f"batch refuses to dispatch {inner_name!r}: nested batch or "
                    f"side-effecting meta-tools are not allowed."
                ))
                continue
            results.append(self.invoke(inner_name, entry.get("args") or {}))
        return tool_ok({"results": results})
