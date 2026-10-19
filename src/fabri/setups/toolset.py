"""Build the tool set a resolved setup declares.

Every layer's ``tools/tools.yaml`` (and ``tools/registered/*.json``) adds
sources and allow entries. Sources are unioned by name (a deeper layer's
source with the same name replaces the shallower one), allow entries are
unioned, and a built tool is kept only when

1. some layer's allow entry matches its name or an alias, and
2. the caller's global allowlist (``ToolPolicy.allow``) matches it too.

So a layer can narrow but never widen what the caller allows. Each tool keeps
its approval class, its kind, the source that built it and the layer that
declared it.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from fabri.setups.layers import LayerLike
from fabri.setups.resolver import ResolvedSetup, resolve_setup
from fabri.tools.credential_store import CredentialStore
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.openapi import OpenAPIOperation
from fabri.tools.policy import ToolPolicy
from fabri.tools.registry import ToolRegistry
from fabri.tools.sources import build_source_tools

logger = logging.getLogger("fabri.setups.tools")


@dataclass
class ResolvedTool:
    name: str
    manifest: ToolManifest
    handler: Callable[[dict], dict]
    approval: str
    kind: str
    source: str  # the source declaration's name
    layer: str  # the layer that declared the source
    origin: str  # declared | registered
    method: str | None = None
    path: str | None = None
    operation: OpenAPIOperation | None = None
    # The description the source built, before the catalog rewrote it.
    base_description: str = ""

    @property
    def aliases(self) -> list[str]:
        return list(self.manifest.aliases or [])

    def fingerprint(self) -> str:
        payload = {
            "description": self.manifest.description, "input_schema": self.manifest.input_schema,
            "approval": self.approval, "kind": self.kind, "method": self.method, "path": self.path,
            "aliases": self.aliases, "title": self.manifest.title,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def row(self) -> dict[str, Any]:
        return {
            "name": self.name, "approval": self.approval, "kind": self.kind, "origin": self.origin,
            "source": self.source, "layer": self.layer, "method": self.method, "path": self.path,
            "aliases": self.aliases, "fingerprint": self.fingerprint(),
        }


@dataclass
class ResolvedToolSet:
    tools: dict[str, ResolvedTool]
    errors: list[str] = field(default_factory=list)
    content_hash: str = ""
    catalog: Any = None  # fabri.tools.catalog.Catalog, attached by attach_catalog

    def names(self) -> list[str]:
        return list(self.tools)

    def approvals(self) -> dict[str, str]:
        """{tool name: approval class}, for a host's approval gate. Aliases
        map to the same class."""
        out = {}
        for tool in self.tools.values():
            out[tool.name] = tool.approval
            for alias in tool.aliases:
                out.setdefault(alias, tool.approval)
        return out

    def approval_for(self, name: str) -> str | None:
        return self.approvals().get(name)

    def get(self, name: str) -> ResolvedTool | None:
        if name in self.tools:
            return self.tools[name]
        for tool in self.tools.values():
            if name in tool.aliases:
                return tool
        return None

    def select(self, names: Iterable[str] | None) -> list[ResolvedTool]:
        if names is None:
            return list(self.tools.values())
        out = []
        for name in names:
            tool = self.get(name)
            if tool is not None and tool not in out:
                out.append(tool)
        return out

    def for_type(self, setup: ResolvedSetup, agent_type: str) -> list[ResolvedTool]:
        return self.select(setup.tools_for(agent_type))

    def registry(self, names: Iterable[str] | None = None, *, sandbox_root: str | None = None,
                 meta_tools: bool = False) -> ToolRegistry:
        """A fresh ToolRegistry holding these tools (or the named subset).
        `meta_tools=True` adds find_tools and describe_tool over the
        catalog."""
        registry = ToolRegistry([], sandbox_root=sandbox_root)
        chosen = self.select(names)
        for tool in chosen:
            registry.register_callable(tool.manifest, tool.handler)
        if meta_tools:
            from fabri.tools.catalog import register_meta_tools

            register_meta_tools(registry, self, names=[t.name for t in chosen])
        return registry

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: tool.row() for name, tool in sorted(self.tools.items())}

    def rows(self) -> list[dict[str, Any]]:
        return [tool.row() for _, tool in sorted(self.tools.items())]


def diff_snapshots(old: Mapping[str, Mapping[str, Any]], new: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Names added, removed and changed (fingerprint differs) between two
    ResolvedToolSet.snapshot() values."""
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(n for n in set(old) & set(new)
                          if old[n].get("fingerprint") != new[n].get("fingerprint")),
    }


def _matches(names: Iterable[str], patterns: Iterable[str]) -> bool:
    patterns = list(patterns)
    return any(fnmatch.fnmatchcase(n, p) for n in names for p in patterns)


def _build(setup: ResolvedSetup, policy: ToolPolicy, transport, store, dry_run) -> ResolvedToolSet:
    allow_entries = [a["entry"] for a in setup.tools["allow"]]
    tools: dict[str, ResolvedTool] = {}
    errors: list[str] = []
    for item in setup.tools["sources"]:
        decl = item["decl"]
        label = f"{item['source']}:{decl.get('name')}"
        if decl.get("enabled") is False:
            continue
        try:
            built = build_source_tools(decl, base_dir=Path(item["base_dir"]), policy=policy,
                                       transport=transport, store=store, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 -- one bad source must not sink the rest
            errors.append(f"{label}: {exc}")
            logger.warning("tool source %s failed to build: %s", label, exc)
            continue
        for tool in built:
            names = [tool.manifest.name, *(tool.manifest.aliases or [])]
            if not _matches(names, allow_entries):
                continue
            if not policy.name_allowed(tool.manifest.name, tool.manifest.aliases or ()):
                continue
            if tool.manifest.name in tools and tools[tool.manifest.name].source != tool.source:
                errors.append(f"{label}: tool {tool.manifest.name} also built by source "
                              f"{tools[tool.manifest.name].source}; the deeper one wins")
            tools[tool.manifest.name] = ResolvedTool(
                name=tool.manifest.name, manifest=tool.manifest, handler=tool.handler,
                approval=tool.manifest.approval or "write", kind=tool.kind, source=tool.source,
                layer=item["source"], origin=item.get("origin", "declared"), method=tool.method,
                path=tool.path, operation=tool.operation, base_description=tool.manifest.description,
            )
    return ResolvedToolSet(tools=tools, errors=errors, content_hash=setup.content_hash)


CACHE_MAX = 64
_cache: dict[tuple, ResolvedToolSet] = {}
_cache_lock = threading.Lock()


def resolve_tools(
    setup: ResolvedSetup | Iterable[LayerLike],
    *,
    global_allow: Iterable[str] | None = None,
    policy: ToolPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    store: CredentialStore | None = None,
    dry_run: bool = False,
    use_cache: bool = True,
    catalog: bool = True,
    state_dir: str | Path | None = None,
) -> ResolvedToolSet:
    """Build the tool set for a resolved setup (or a layer list).

    `policy` defaults to ToolPolicy.permissive(); `global_allow`, when given,
    replaces the policy's allow list. None for both means no global name cap,
    only the layers' allow entries. Cached by the setup's content hash, so an
    edited tools.yaml or a newly registered tool shows up on the next call.
    `catalog=True` attaches the tool catalog (titles, descriptions, probe
    options) from `state_dir`, by default the deepest layer's
    ``tools/.fabri-state``.
    """
    resolved = setup if isinstance(setup, ResolvedSetup) else resolve_setup(setup)
    pol = policy or ToolPolicy.permissive()
    if global_allow is not None:
        pol = pol.with_allow(global_allow)
    key = (tuple((l.name, str(l.path)) for l in resolved.layers), resolved.content_hash, repr(pol),
           json.dumps(pol.dry_run_header, sort_keys=True, default=str), dry_run, id(transport), id(store), catalog,
           str(state_dir or ""), _state_stamp(resolved, state_dir) if catalog else "")
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            return hit
    result = _build(resolved, pol, transport, store, dry_run)
    if catalog:
        from fabri.tools.catalog import attach_catalog

        attach_catalog(result, resolved, state_dir=state_dir)
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > CACHE_MAX:
            _cache.pop(next(iter(_cache)))
    return result


def default_state_dir(setup: ResolvedSetup) -> Path | None:
    """Where sync snapshots, probe results and description caches live: the
    deepest layer's ``tools/.fabri-state``. Hidden, so it never changes the
    content hash."""
    if not setup.layers:
        return None
    return setup.layers[-1].path / "tools" / ".fabri-state"


def _state_stamp(setup: ResolvedSetup, state_dir: str | Path | None) -> str:
    directory = Path(state_dir) if state_dir else default_state_dir(setup)
    if directory is None or not directory.is_dir():
        return ""
    parts = []
    for path in sorted(directory.glob("*.json")):
        stat = path.stat()
        parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
