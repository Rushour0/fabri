from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from fabri.core.hooks import Reject

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_error, tool_ok
from fabri.tools.run_context import (
    CallContext,
    RunContext,
    batch_entry_call,
    call_scope,
    current_call,
    current_run,
)

if TYPE_CHECKING:
    from fabri.sandbox import Sandbox

ToolHandler = Callable[[dict], dict]

BATCH_TOOL_NAME = "batch"
# Nested batches multiply fan-out unpredictably; spawn_subagent + ask_user
# have side effects that don't belong inside an opaque batch result.
BATCH_FORBIDDEN_NESTED = frozenset({BATCH_TOOL_NAME, "spawn_subagent", "ask_user"})
# The loop handles decompose itself and a missing batch manifest is a config
# problem, so neither name is offered to the host's on_unknown_tool hook.
_UNKNOWN_HOOK_EXCLUDED = frozenset({BATCH_TOOL_NAME, "decompose"})


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
        # Names a tool asked the agent loop to expose mid-run (see
        # request_exposure). Drained by the loop after each tool step.
        self._exposed: list[str] = []
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

    def canonical_name(self, name: str) -> str | None:
        """Return the registered name for `name`, following manifest aliases
        (for example an OpenAPI operation_id). None when nothing matches."""
        if name in self.tools:
            return name
        for manifest in self.tools.values():
            if name in (manifest.aliases or ()):
                return manifest.name
        return None

    def request_exposure(self, names: list[str]) -> None:
        """Ask the agent loop to add these tools to the provider tool list
        for the rest of the run. Used by discovery tools such as find_tools
        when tool retrieval narrowed the visible set."""
        for name in names:
            canonical = self.canonical_name(name)
            if canonical and canonical not in self._exposed:
                self._exposed.append(canonical)

    def pop_exposed(self) -> list[str]:
        names, self._exposed = self._exposed, []
        return names

    def invoke(self, name: str, args: dict) -> dict:
        """Run one tool call through a fixed pipeline:

        1. ``batch`` goes to ``invoke_batch``; each entry re-enters here.
        2. Resolve the name. Unknown names go to ``hooks.on_unknown_tool``,
           then to the ``unknown tool`` error.
        3. Deferred-not-loaded check (slot, empty).
        4. Ledger dedup lookup (slot, empty).
        5. Approval gate (slot, empty).
        6. ``hooks.before_tool``.
        7. The handler.
        8. Ledger record (slot, empty).
        9. ``hooks.after_tool``.

        Steps 2-9 read the active RunContext. With none active (a host calling
        invoke directly) only step 1, the name check and the handler run, as
        before run contexts existed."""
        if name == BATCH_TOOL_NAME and BATCH_TOOL_NAME in self.tools:
            return self.invoke_batch(args.get("calls") or [])
        run = current_run()
        hooks = run.hooks if run is not None else None
        canonical = self.canonical_name(name)
        if canonical is None:
            on_unknown = getattr(hooks, "on_unknown_tool", None)
            if on_unknown is not None and name not in _UNKNOWN_HOOK_EXCLUDED:
                try:
                    handled = on_unknown(name, args)
                except Exception as e:
                    return tool_error(f"{name}: on_unknown_tool hook raised {type(e).__name__}: {e}")
                if handled is not None:
                    return handled
            return tool_error(f"unknown tool: {name}")
        if run is None:
            return self._run_handler(canonical, args)
        call = self._call_context(run, name, canonical, args)
        with call_scope(call):
            # 3. deferred-not-loaded check: slot for deferred tool loading.
            # 4. ledger dedup lookup: slot for the write ledger.
            # 5. approval gate: slot for the approval gate.
            # 6. host before_tool.
            before = getattr(hooks, "before_tool", None)
            result = None
            if before is not None:
                try:
                    verdict = before(call)
                except Exception as e:
                    return tool_error(f"{canonical}: before_tool hook raised {type(e).__name__}: {e}")
                if isinstance(verdict, Reject):
                    result = verdict.result if verdict.result is not None else tool_error(
                        f"{canonical}: rejected: {verdict.reason}"
                    )
            # 7. handler.
            if result is None:
                result = self._run_handler(canonical, args)
                # 8. ledger record: slot for the write ledger.
            # 9. host after_tool.
            after = getattr(hooks, "after_tool", None)
            if after is not None:
                try:
                    replaced = after(call, result)
                except Exception as e:
                    return tool_error(f"{canonical}: after_tool hook raised {type(e).__name__}: {e}")
                if isinstance(replaced, dict):
                    result = replaced
            return result

    @staticmethod
    def _call_context(run: RunContext, name: str, canonical: str, args: dict) -> CallContext:
        """The active CallContext for this call with the resolved name filled in.
        A tool invoked from inside another tool (no matching context) gets a
        fresh CallContext that carries only the run."""
        call = current_call()
        if call is None or call.invoked_name != name:
            call = CallContext(run=run, invoked_name=name, args=args)
        return replace(call, run=run, canonical_name=canonical, args=args)

    def _run_handler(self, name: str, args: dict) -> dict:
        manifest = self.tools[name]
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
        for index, entry in enumerate(calls):
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
            inner_args = entry.get("args") or {}
            if current_run() is None:
                results.append(self.invoke(inner_name, inner_args))
                continue
            with call_scope(batch_entry_call(index, inner_name, inner_args)):
                results.append(self.invoke(inner_name, inner_args))
        return tool_ok({"results": results})
