"""Per-spawn tool scope for sub-agents.

A parent can ask that a spawned child see fewer tools than the child's config
gives it. The scope only narrows: it is intersected with the tools the child's
own config builds, and (when the parent's tool names are known) with the
parent's tools too, so a child never gains a tool through it.

A scope is a list of entries. Each entry is a tool name or the preset
``read_only``, which expands to the read-class tools of the registry it is
applied to. Names that match nothing are dropped and reported.

The approval gate is untouched: narrowing removes tools from the registry and
leaves its approver as it was, so every remaining ``always_approve`` tool still
asks the parent's human (or is refused when there is none).
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Iterable, Mapping

if TYPE_CHECKING:
    from fabri.tools.manifest_schema import ToolManifest
    from fabri.tools.registry import ToolRegistry

READ_ONLY_PRESET = "read_only"
PRESETS = frozenset({READ_ONLY_PRESET})

# The parent's registry hands its tool names to a spawn_subagent subprocess in
# this env var (JSON list), so the spawn can drop names the parent lacks.
PARENT_TOOLS_ENV = "FABRI_PARENT_TOOLS"

# Bundled tools that only read. Manifests without an `approval` field fall back
# to this list; a manifest that says `approval: read` counts as read too.
READ_ONLY_BUILTINS = frozenset({
    "read_file", "grep", "list_dir", "file_stats", "regex_lines",
    "fetch_url", "web_search", "echo_tool", "sum_tool", "find_tools",
})


def is_read_tool(name: str, manifest: "ToolManifest | None") -> bool:
    """True when the tool has no side effects as far as fabri can tell."""
    if manifest is not None and manifest.approval is not None:
        return manifest.approval == "read"
    return name in READ_ONLY_BUILTINS


def normalize_scope(raw: object) -> list[str] | None:
    """A clean list of scope entries, or None when no scope was given. A bare
    string is one entry. Anything that is not a string is ignored."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def parent_tool_names(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The parent's tool names from PARENT_TOOLS_ENV, or None when unknown."""
    raw = (os.environ if env is None else env).get(PARENT_TOOLS_ENV)
    if not raw:
        return None
    try:
        names = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(names, list):
        return None
    return frozenset(n for n in names if isinstance(n, str))


def filter_to_parent(scope: list[str], parent: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """Split ``scope`` into (kept, dropped) against the parent's tools. Presets
    are kept: the child expands them against its own registry, and the
    child's registry is itself intersected with the scope."""
    if parent is None:
        return list(scope), []
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


def narrow_registry(registry: "ToolRegistry", scope: list[str] | None) -> dict:
    """Remove every tool the scope does not allow from ``registry`` in place.
    ``None`` leaves it alone. Returns a small report for the child's output."""
    if scope is None:
        return {}
    allowed, unmatched = resolve_scope(scope, registry.tools)
    removed = sorted(n for n in registry.tools if n not in allowed)
    for name in removed:
        registry.tools.pop(name, None)
        registry._callables.pop(name, None)
    return {"requested": list(scope), "kept": sorted(registry.tools), "unmatched": unmatched}


def scope_is_read_only(scope: object, tools: Mapping[str, "ToolManifest"]) -> bool:
    """True when a requested scope can only ever give the child read tools.
    An absent scope is not read-only: the child keeps its whole config."""
    entries = normalize_scope(scope)
    if not entries:
        return False
    for entry in entries:
        if entry == READ_ONLY_PRESET:
            continue
        if not is_read_tool(entry, tools.get(entry)):
            return False
    return True
