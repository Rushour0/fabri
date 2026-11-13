"""Per-spawn tool scope for sub-agents.

A parent can ask that a spawned child see fewer tools than the child's config
gives it. The scope only narrows:

- the spawn drops entries the parent's own registry lacks, and refuses when it
  cannot tell what the parent has (fail closed);
- the child intersects the scope with the tools its own config builds;
- the child then records the names it kept as a ceiling in
  ``FABRI_TOOL_CEILING``. Every descendant (spawned, or a static
  ``tools.agents[]`` child) inherits that env var and intersects its own tools
  with it, so a grandchild never sees more than its scoped parent.

A scope is a list of entries. Each entry is a tool name or the preset
``read_only``, which expands to the read-class tools of the registry it is
applied to.

The approval gate is untouched: narrowing removes tools from the registry and
leaves its approver as it was, so every remaining ``always_approve`` tool still
asks the parent's human (or is refused when there is none).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

if TYPE_CHECKING:
    from fabri.tools.manifest_schema import ToolManifest
    from fabri.tools.registry import ToolRegistry

READ_ONLY_PRESET = "read_only"
PRESETS = frozenset({READ_ONLY_PRESET})

# The parent's registry hands its tool names to a spawn subprocess in this env
# var (JSON list), so the spawn can drop names the parent lacks.
PARENT_TOOLS_ENV = "FABRI_PARENT_TOOLS"
# Names a scoped ancestor kept (JSON list). Inherited by every descendant.
CEILING_ENV = "FABRI_TOOL_CEILING"

# Bundled tools that only read local data. They count as read only when the
# manifest really runs fabri's bundled script, so a config tool that reuses
# one of these names does not. A manifest that says `approval: read` counts
# as read too. Network tools (fetch_url, web_search) are left out: they can
# send data out, so a scope that includes them is treated as one that writes.
READ_ONLY_BUILTINS = frozenset({
    "read_file", "grep", "list_dir", "file_stats", "regex_lines", "echo_tool", "sum_tool",
})
_EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"


class ScopeError(ValueError):
    """A tool_scope value that is not a string or a list of strings, or one
    the spawn cannot prove narrows the parent's tools."""


def _is_bundled(manifest: "ToolManifest") -> bool:
    for part in manifest.command or ():
        try:
            if Path(str(part)).resolve().is_relative_to(_EXAMPLES_DIR):
                return True
        except (OSError, ValueError):
            continue
    return False


def is_read_tool(name: str, manifest: "ToolManifest | None") -> bool:
    """True when the tool has no side effects as far as fabri can tell.
    Unknown tools (no manifest) are not read."""
    if manifest is None:
        return False
    if manifest.approval is not None:
        return manifest.approval == "read"
    return name in READ_ONLY_BUILTINS and _is_bundled(manifest)


def normalize_scope(raw: object) -> list[str] | None:
    """A clean list of scope entries, or None when no scope was given.

    A non-empty string is one entry. A list must hold only non-empty strings.
    Anything else (a dict, a number, a bool, an empty string, a list with a
    non-string) raises :class:`ScopeError`, so a malformed scope never turns
    into an unscoped child."""
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            raise ScopeError("tool_scope must not be an empty string")
        return [raw.strip()]
    if not isinstance(raw, list):
        raise ScopeError(f"tool_scope must be a list of tool names, got {type(raw).__name__}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ScopeError(f"tool_scope entries must be non-empty strings, got {item!r}")
        if item.strip() not in out:
            out.append(item.strip())
    return out


def _names_from_env(var: str, env: Mapping[str, str]) -> frozenset[str] | None:
    raw = env.get(var)
    if raw is None:
        return None
    try:
        names = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    return frozenset(names)


def parent_tool_names(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The parent's tool names from PARENT_TOOLS_ENV, or None when absent or
    unreadable. Callers with a scope must treat None as "refuse"."""
    return _names_from_env(PARENT_TOOLS_ENV, os.environ if env is None else env)


def ceiling_names(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The inherited ceiling, or None when no scoped ancestor set one. A set
    but unreadable value is an empty ceiling (no tools), not "no ceiling"."""
    source = os.environ if env is None else env
    if CEILING_ENV not in source:
        return None
    names = _names_from_env(CEILING_ENV, source)
    return names if names is not None else frozenset()


def filter_to_parent(scope: list[str], parent: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """Split ``scope`` into (kept, dropped) against the parent's tools. Presets
    are kept: the child expands them against its own registry, and the
    child's registry is itself intersected with the scope. ``parent=None``
    (unknown) raises :class:`ScopeError`: without the parent's list the
    spawn cannot prove the scope narrows."""
    if parent is None:
        raise ScopeError("tool_scope was given but the parent's tool list is unknown; refusing to spawn")
    parent_set = set(parent)
    kept, dropped = [], []
    for entry in scope:
        (kept if entry in PRESETS or entry in parent_set else dropped).append(entry)
    return kept, dropped


def resolve_scope(scope: list[str], tools: Mapping[str, "ToolManifest"]) -> tuple[set[str], list[str]]:
    """The tool names in ``tools`` the scope allows, and the entries that
    matched nothing there."""
    allowed: set[str] = set()
    unmatched: list[str] = []
    for entry in scope:
        if entry == READ_ONLY_PRESET:
            allowed.update(n for n, m in tools.items() if is_read_tool(n, m))
        elif entry in tools:
            allowed.add(entry)
        else:
            unmatched.append(entry)
    return allowed, unmatched


def _keep_only(registry: "ToolRegistry", keep: set[str]) -> None:
    for name in [n for n in registry.tools if n not in keep]:
        registry.tools.pop(name, None)
        registry._callables.pop(name, None)


def narrow_registry(registry: "ToolRegistry", scope: list[str] | None) -> dict:
    """Remove every tool the scope does not allow from ``registry`` in place.
    ``None`` leaves it alone. Returns a small report for the child's output."""
    if scope is None:
        return {}
    allowed, unmatched = resolve_scope(scope, registry.tools)
    _keep_only(registry, allowed)
    return {"requested": list(scope), "kept": sorted(registry.tools), "unmatched": unmatched}


def apply_child_scope(registry: "ToolRegistry", scope: list[str] | None,
                      env: dict | None = None) -> dict:
    """What a child runner does right after ``build_tools``: narrow by its own
    scope, then by any inherited ceiling, then publish what is left as the
    ceiling for its descendants (in ``env``, default ``os.environ``). With no
    scope and no ceiling nothing changes and no ceiling is set."""
    target = os.environ if env is None else env
    report = narrow_registry(registry, scope)
    ceiling = ceiling_names(target)
    if ceiling is not None:
        before = set(registry.tools)
        _keep_only(registry, set(ceiling))
        report = {**report, "ceiling_removed": sorted(before - set(registry.tools)),
                  "kept": sorted(registry.tools)}
    if scope is not None or ceiling is not None:
        target[CEILING_ENV] = json.dumps(sorted(registry.tools))
    return report


_CONFIG_READ_ONLY_CACHE: dict[tuple[str, int, int], bool] = {}


def child_config_is_read_only(config_path: object) -> bool:
    """True only when the child config at ``config_path`` loads and every tool
    it builds is read-class, with no spawn tool, no ``tools.agents[]``, no
    MCP servers and no ``tools.sources``. Anything else, including a load
    failure, is False. Results are cached by path, mtime and size."""
    if not isinstance(config_path, str) or not config_path:
        return False
    try:
        path = Path(config_path).resolve()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except (OSError, ValueError):
        return False
    if key in _CONFIG_READ_ONLY_CACHE:
        return _CONFIG_READ_ONLY_CACHE[key]
    result = False
    try:
        from fabri.config import load_config
        from fabri.runtime import build_tools
        from fabri.tools.registry import is_spawn_tool

        tools_cfg = load_config(str(path))["tools"]
        if not (tools_cfg.get("agents") or tools_cfg.get("mcp_servers") or tools_cfg.get("sources")):
            registry = build_tools(tools_cfg)
            result = bool(registry.tools) and all(
                is_read_tool(n, m) and not is_spawn_tool(n, m) for n, m in registry.tools.items())
    except Exception:  # noqa: BLE001 -- fail closed
        result = False
    _CONFIG_READ_ONLY_CACHE[key] = result
    return result


def spawn_is_reader(args: object, tools: Mapping[str, "ToolManifest"]) -> bool:
    """Whether a spawn_subagent call can only read. With a ``tool_scope`` the
    scope decides (:func:`scope_is_read_only`). Without one the child gets its
    whole config, so it reads only when :func:`child_config_is_read_only`."""
    if not isinstance(args, dict):
        return False
    if "tool_scope" in args:
        return scope_is_read_only(args["tool_scope"], tools)
    return child_config_is_read_only(args.get("config_path"))


def scope_is_read_only(scope: object, tools: Mapping[str, "ToolManifest"]) -> bool:
    """True when a requested scope can only ever give the child read tools,
    judged against the parent's registry ``tools``. No scope, a malformed
    scope, or any entry that is unknown here or not read-class makes the
    child a writer."""
    try:
        entries = normalize_scope(scope)
    except ScopeError:
        return False
    if not entries:
        return False
    for entry in entries:
        if entry == READ_ONLY_PRESET:
            continue
        if not is_read_tool(entry, tools.get(entry)):
            return False
    return True
