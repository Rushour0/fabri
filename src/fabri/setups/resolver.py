"""Resolve an ordered list of layer directories into one setup.

Each layer has the same shape (every part optional):

    <layer>/
      setup.yaml                    deep merged, deeper scalars win
      agents/<type>/agent.yaml      deep merged; `tools` and `skills` lists union unless in `replace`
      agents/<type>/prompt.md       replaces the prompt so far
      agents/<type>/prompt.append.md   appended to the prompt so far
      skills/<name>/skill.yaml      + optional prompt.md; a deeper skill replaces a shallower one
      context/<name>.md             always-on context; frontmatter `types: [...]` narrows it
      data/<name>.<ext>             reference data, not injected
      tools/tools.yaml              `sources:` and `allow:`, unioned across layers
      tools/sources/<name>.yaml     the same shape, one file per added source
      tools/registered/<name>.json  tools added through fabri.tools.registration
      plays/<name>/play.yaml        a deeper play replaces a shallower one

A marker file ``<kind>/<name>.disabled`` (agents, skills, context, data,
plays) or ``enabled: false`` removes an item that a shallower layer added.
Every resolved item records the layer it came from.

The resolver knows nothing about what the layers mean. A host passes them in
order, for example ``[baseline, pack-a, pack-b, org, project]``.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fabri.setups.layers import (
    DEFAULT_BUDGETS,
    Layer,
    LayerLike,
    SetupError,
    as_layers,
    content_hash,
    deep_merge,
    is_hidden,
    read_yaml,
    split_frontmatter,
)

AGENT_FILES = ("agent.yaml", "prompt.md", "prompt.append.md")
LIST_UNION_KEYS = ("tools", "skills")


@dataclass
class ResolvedSetup:
    layers: list[Layer]
    setup: dict[str, Any]
    agents: dict[str, dict[str, Any]]
    skills: dict[str, dict[str, Any]]
    context: dict[str, dict[str, Any]]
    data: dict[str, dict[str, Any]]
    plays: dict[str, dict[str, Any]]
    tools: dict[str, Any]
    sources: dict[str, str]
    content_hash: str
    warnings: list[str] = field(default_factory=list)

    # -- per type views -------------------------------------------------

    @property
    def budgets(self) -> dict[str, int]:
        raw = self.setup.get("budgets") or {}
        out = dict(DEFAULT_BUDGETS)
        if isinstance(raw, Mapping):
            out.update({k: v for k, v in raw.items() if k in DEFAULT_BUDGETS and isinstance(v, int)})
        return out

    def agent_types(self) -> list[str]:
        return [name for name, item in self.agents.items() if item.get("enabled", True)]

    def agent(self, agent_type: str) -> dict[str, Any]:
        item = self.agents.get(agent_type)
        if item is None or not item.get("enabled", True):
            raise SetupError(f"agent type {agent_type!r} is not defined or is disabled")
        return item

    def route_options(self) -> list[dict[str, Any]]:
        """Agent types a host router can offer: every enabled type whose
        merged agent.yaml has a `route` block with `description` and
        `not_for`, in the order the types first appeared across layers."""
        out = []
        for name in self.agent_types():
            route = self.agents[name]["config"].get("route")
            if isinstance(route, Mapping) and route.get("description") and route.get("not_for"):
                out.append({"type": name, "description": str(route["description"]),
                            "not_for": str(route["not_for"]), "source": self.agents[name]["source"]})
        return out

    def skills_for(self, agent_type: str) -> list[str]:
        """Enabled skills whose `types` is empty or names this type, plus any
        skill the type's agent.yaml lists under `skills`."""
        forced = set(self.agents.get(agent_type, {}).get("config", {}).get("skills") or [])
        out = []
        for name, skill in self.skills.items():
            types = skill.get("types") or []
            if name in forced or not types or agent_type in types:
                out.append(name)
        return out

    def skill_load_mode(self, name: str) -> str:
        """`always` or `on_demand`. A skill's own `load` wins, then an
        `always` tag, then setup.yaml `skill_load`, then `always`."""
        skill = self.skills[name]
        if skill.get("load"):
            return skill["load"]
        if "always" in (skill.get("tags") or []):
            return "always"
        return "on_demand" if self.setup.get("skill_load") == "on_demand" else "always"

    def skill_index_line(self, name: str) -> str:
        skill = self.skills[name]
        line = f"- `{name}`: {' '.join(str(skill.get('description') or '').split())}"
        if skill.get("when_to_use"):
            line += f" Use when: {' '.join(skill['when_to_use'].split())}"
        if skill.get("references"):
            line += f" ({len(skill['references'])} reference file{'s' if len(skill['references']) != 1 else ''})"
        return line

    def skill_modes(self, agent_type: str) -> dict[str, tuple[str, str | None]]:
        """``{name: (mode, reason)}`` for the type's skills after budgets.
        An always-loaded skill over `skill_prompt_chars`, or one that would
        push the type's always-loaded skills past `skills_total_chars`, moves
        to on demand so it stays reachable through load_skill instead of
        bloating the prompt. `reason` says why a skill moved, else None."""
        budgets = self.budgets
        used, out = 0, {}
        for name in self.skills_for(agent_type):
            mode = self.skill_load_mode(name)
            reason = None
            if mode == "always":
                size = len((self.skills[name].get("prompt") or "").strip())
                if size > budgets["skill_prompt_chars"]:
                    mode, reason = "on_demand", f"prompt is {size} chars, budget {budgets['skill_prompt_chars']}"
                elif used + size > budgets["skills_total_chars"]:
                    mode, reason = "on_demand", (f"always-loaded skills would reach {used + size} chars, "
                                                 f"budget {budgets['skills_total_chars']}")
                else:
                    used += size
            out[name] = (mode, reason)
        return out

    def on_demand_skills(self, agent_type: str) -> list[str]:
        return [n for n, (mode, _) in self.skill_modes(agent_type).items() if mode == "on_demand"]

    def load_skill(self, agent_type: str, name: str, reference: str | None = None) -> dict[str, Any]:
        """Full text of one skill (or one of its reference files) for a type.
        Raises SetupError for a skill the type does not load or a reference
        the skill does not list. Text is cut at `skill_ondemand_chars`."""
        if name not in self.skills_for(agent_type):
            raise SetupError(f"no skill named {name!r} for agent type {agent_type!r}")
        skill = self.skills[name]
        limit = self.budgets["skill_ondemand_chars"]
        if reference is None:
            text = (skill.get("prompt") or "").strip()
            out: dict[str, Any] = {"name": name, "revision": skill.get("revision", 0),
                                   "tools": skill.get("tools") or [], "references": skill.get("references") or []}
        else:
            if reference not in (skill.get("references") or []):
                raise SetupError(f"skill {name!r} has no reference {reference!r}; "
                                 f"it lists {skill.get('references') or []}")
            text = (Path(skill["path"]) / reference).read_text(encoding="utf-8")
            out = {"name": name, "reference": reference, "revision": skill.get("revision", 0)}
        out["truncated"] = len(text) > limit
        out["text"] = text[:limit]
        return out

    def context_for(self, agent_type: str | None) -> dict[str, dict[str, Any]]:
        if agent_type is None:
            return dict(self.context)
        return {n: c for n, c in self.context.items() if not c.get("types") or agent_type in c["types"]}

    def tools_for(self, agent_type: str) -> list[str] | None:
        """Tool names this type asks for: agent.yaml `tools` plus the tools
        of its skills. None when agent.yaml has no `tools` key, which means
        every resolved tool."""
        config = self.agents.get(agent_type, {}).get("config", {})
        if "tools" not in config:
            return None
        names = list(config.get("tools") or [])
        for skill in self.skills_for(agent_type):
            names.extend(self.skills[skill].get("tools") or [])
        return list(dict.fromkeys(names))

    def system_prompt(self, agent_type: str, base: str = "") -> tuple[str, list[str]]:
        """The type's prompt, then its skills, then its context, inside the
        budgets. A skill over its budget is listed on demand (see
        skill_modes); context is cut
        at the last whole file that fits `context_chars`. Returns the text
        and the warnings."""
        budgets = self.budgets
        warnings: list[str] = []
        agent = self.agent(agent_type)
        parts = [p for p in (base.strip(), agent.get("prompt", "").strip()) if p]
        skill_parts, index_lines = [], []
        for name, (mode, reason) in self.skill_modes(agent_type).items():
            if mode == "on_demand":
                if reason:
                    warnings.append(f"skill {name} moved to on demand: {reason}")
                index_lines.append(self.skill_index_line(name))
                continue
            text = (self.skills[name].get("prompt") or "").strip()
            if text:
                skill_parts.append(f"### {name}\n{text}")
        if skill_parts:
            parts.append("## Skills\n\n" + "\n\n".join(skill_parts))
        if index_lines:
            parts.append("## Skills to load on demand\n\n"
                         "Each skill below holds detailed instructions that are not in this prompt. "
                         "Call `load_skill` with the skill name when a task matches its description, "
                         "before you act on that task.\n\n" + "\n".join(index_lines))
        used, context_parts = 0, []
        for name, item in self.context_for(agent_type).items():
            section = f"### {name}\n{item['text'].strip()}"
            if used + len(section) > budgets["context_chars"]:
                warnings.append(f"context cut at {name}: over the {budgets['context_chars']} char budget")
                break
            context_parts.append(section)
            used += len(section)
        if context_parts:
            parts.append("## Context\n\n" + "\n\n".join(context_parts))
        return "\n\n".join(parts), warnings

    def read_data(self, name: str) -> bytes:
        item = self.data.get(name)
        if item is None:
            raise SetupError(f"no data item named {name!r}")
        return Path(item["path"]).read_bytes()

    def to_dict(self, agent_type: str | None = None) -> dict[str, Any]:
        """A JSON-ready view with the source layer of every item. With
        `agent_type`, narrowed to what that type loads."""
        def strip(item: Mapping[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in item.items() if k != "path"}

        out: dict[str, Any] = {
            "layers": [{"name": layer.name, "path": str(layer.path)} for layer in self.layers],
            "content_hash": self.content_hash,
            "setup": self.setup,
            "routes": self.route_options(),
            "tools": {
                "sources": [{"name": s["decl"].get("name"), "kind": s["decl"].get("kind"),
                             "source": s["source"], "origin": s["origin"]} for s in self.tools["sources"]],
                "allow": self.tools["allow"],
            },
            "sources": self.sources,
            "warnings": self.warnings,
        }
        if agent_type is None:
            out["agents"] = {n: strip(a) for n, a in self.agents.items()}
            out["skills"] = {n: strip(s) for n, s in self.skills.items()}
            out["context"] = {n: strip(c) for n, c in self.context.items()}
        else:
            agent = self.agent(agent_type)
            prompt, warnings = self.system_prompt(agent_type)
            out["agent"] = {"type": agent_type, **strip(agent)}
            out["skills"] = {n: {**strip(self.skills[n]), "load_mode": mode}
                             for n, (mode, _) in self.skill_modes(agent_type).items()}
            out["context"] = {n: strip(c) for n, c in self.context_for(agent_type).items()}
            out["tools_for_type"] = self.tools_for(agent_type)
            out["system_prompt"] = prompt
            out["warnings"] = [*self.warnings, *warnings]
        out["data"] = {n: {k: v for k, v in d.items() if k != "path"} for n, d in self.data.items()}
        out["plays"] = {n: strip(p) for n, p in self.plays.items()}
        return out


# ---------------------------------------------------------------- resolving


def _disabled(directory: Path, name: str) -> bool:
    return (directory / f"{name}.disabled").exists()


def _revision(layer_root: Path, name: str) -> int:
    from fabri.setups.store import item_revision

    return item_revision(layer_root, "skills", name)


def _children(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if not p.name.startswith("."))


def _resolve_uncached(layers: list[Layer], digest: str) -> ResolvedSetup:
    setup: dict[str, Any] = {}
    agents: dict[str, dict[str, Any]] = {}
    skills: dict[str, dict[str, Any]] = {}
    context: dict[str, dict[str, Any]] = {}
    data: dict[str, dict[str, Any]] = {}
    plays: dict[str, dict[str, Any]] = {}
    tool_sources: dict[str, dict[str, Any]] = {}
    descriptions: dict[str, dict[str, Any]] = {}
    allow: list[dict[str, str]] = []
    sources: dict[str, str] = {}
    warnings: list[str] = []

    for layer in layers:
        root = layer.path
        if not root.is_dir():
            warnings.append(f"layer {layer.name}: {root} does not exist")
            continue
        layer_setup = read_yaml(root / "setup.yaml")
        if layer_setup:
            setup = deep_merge(setup, layer_setup)
            sources["setup.yaml"] = layer.name

        # agents
        for path in _children(root / "agents"):
            if path.is_file() and path.suffix == ".disabled":
                if path.stem in agents:
                    agents[path.stem]["enabled"] = False
                    agents[path.stem]["source"] = layer.name
                continue
            if not path.is_dir():
                continue
            name = path.name
            current = agents.get(name) or {"config": {}, "prompt": "", "prompt_sources": [],
                                           "sources": [], "enabled": True}
            new = read_yaml(path / "agent.yaml")
            if (path / "agent.yaml").exists():
                replace = set(new.get("replace") or [])
                merged = deep_merge(current["config"], new)
                for key in LIST_UNION_KEYS:
                    if key in new and key not in replace:
                        merged[key] = list(dict.fromkeys([*(current["config"].get(key) or []), *(new.get(key) or [])]))
                merged.pop("replace", None)
                current["config"] = merged
                current["enabled"] = merged.get("enabled", True) is not False
                sources[f"agents/{name}/agent.yaml"] = layer.name
            if (path / "prompt.md").exists():
                current["prompt"] = (path / "prompt.md").read_text(encoding="utf-8")
                current["prompt_sources"] = [layer.name]
                sources[f"agents/{name}/prompt.md"] = layer.name
            if (path / "prompt.append.md").exists():
                extra = (path / "prompt.append.md").read_text(encoding="utf-8")
                current["prompt"] = (current["prompt"].rstrip() + "\n\n" + extra) if current["prompt"] else extra
                current["prompt_sources"] = [*current["prompt_sources"], layer.name]
                sources[f"agents/{name}/prompt.append.md"] = layer.name
            current["source"] = layer.name
            current["sources"] = list(dict.fromkeys([*current["sources"], layer.name]))
            current["route"] = current["config"].get("route")
            if _disabled(root / "agents", name):
                current["enabled"] = False
            agents[name] = current

        # skills
        for path in _children(root / "skills"):
            if path.is_file() and path.suffix == ".disabled":
                skills.pop(path.stem, None)
                sources[f"skills/{path.stem}"] = layer.name
                continue
            if not path.is_dir() or not (path / "skill.yaml").exists():
                continue
            meta = read_yaml(path / "skill.yaml")
            if meta.get("name") not in (None, path.name):
                warnings.append(f"layer {layer.name}: skills/{path.name}/skill.yaml name does not match its directory")
                continue
            if meta.get("enabled") is False or _disabled(root / "skills", path.name):
                skills.pop(path.name, None)
            else:
                prompt = (path / "prompt.md").read_text(encoding="utf-8") if (path / "prompt.md").exists() else ""
                refs_dir = path / "references"
                references = sorted(f"references/{p.name}" for p in refs_dir.iterdir()
                                    if p.is_file() and not p.name.startswith(".")) if refs_dir.is_dir() else []
                skills[path.name] = {**meta, "name": path.name, "types": list(meta.get("types") or []),
                                     "tools": list(meta.get("tools") or []),
                                     "tags": [str(t) for t in meta.get("tags") or []],
                                     "when_to_use": str(meta.get("when_to_use") or ""),
                                     "load": meta.get("load") if meta.get("load") in ("always", "on_demand") else None,
                                     "references": references, "revision": _revision(layer.path, path.name),
                                     "prompt": prompt, "source": layer.name, "path": str(path)}
            sources[f"skills/{path.name}"] = layer.name

        # context
        for path in _children(root / "context"):
            if path.suffix == ".disabled":
                context.pop(path.stem, None)
                sources[f"context/{path.stem}"] = layer.name
                continue
            if path.suffix != ".md" or not path.is_file():
                continue
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
            if meta.get("enabled") is False or _disabled(root / "context", path.stem):
                context.pop(path.stem, None)
            else:
                context[path.stem] = {"text": body, "types": list(meta.get("types") or []),
                                      "source": layer.name, "path": str(path)}
            sources[f"context/{path.stem}"] = layer.name

        # data
        for path in _children(root / "data"):
            if not path.is_file():
                continue
            if path.suffix == ".disabled":
                data.pop(path.stem, None)
                sources[f"data/{path.stem}"] = layer.name
                continue
            if _disabled(root / "data", path.stem):
                continue
            data[path.stem] = {"file": path.name, "bytes": path.stat().st_size,
                               "source": layer.name, "path": str(path)}
            sources[f"data/{path.stem}"] = layer.name

        # plays
        for path in _children(root / "plays"):
            if path.is_file() and path.suffix == ".disabled":
                plays.pop(path.stem, None)
                continue
            if not path.is_dir() or not (path / "play.yaml").exists():
                continue
            play = read_yaml(path / "play.yaml")
            if play.get("enabled") is False or _disabled(root / "plays", path.name):
                plays.pop(path.name, None)
            elif play.get("name") not in (None, path.name):
                warnings.append(f"layer {layer.name}: plays/{path.name}/play.yaml name does not match its directory")
            else:
                plays[path.name] = {**play, "name": path.name, "source": layer.name, "path": str(path)}
            sources[f"plays/{path.name}"] = layer.name

        # tools
        tools_yaml = read_yaml(root / "tools" / "tools.yaml")
        for decl in tools_yaml.get("sources") or []:
            if not isinstance(decl, Mapping) or not decl.get("name"):
                warnings.append(f"layer {layer.name}: tools.yaml has a source without a name")
                continue
            tool_sources[str(decl["name"])] = {"decl": dict(decl), "source": layer.name,
                                               "base_dir": str(root / "tools"), "origin": "declared"}
            sources[f"tools/sources/{decl['name']}"] = layer.name
        for entry in tools_yaml.get("allow") or []:
            allow.append({"entry": str(entry), "source": layer.name})
        declared_here = {str(d.get("name")) for d in tools_yaml.get("sources") or [] if isinstance(d, Mapping)}
        for path in sorted((root / "tools" / "sources").glob("*.yaml")) if (root / "tools" / "sources").is_dir() else []:
            if path.name.startswith("."):
                continue
            entry_yaml = read_yaml(path)
            for decl in entry_yaml.get("sources") or []:
                if not isinstance(decl, Mapping) or not decl.get("name"):
                    warnings.append(f"layer {layer.name}: tools/sources/{path.name} has a source without a name")
                    continue
                if str(decl["name"]) in declared_here:
                    warnings.append(f"layer {layer.name}: source {decl['name']} is in tools.yaml and "
                                    f"tools/sources/{path.name}; the sources file wins")
                declared_here.add(str(decl["name"]))
                tool_sources[str(decl["name"])] = {"decl": dict(decl), "source": layer.name,
                                                   "base_dir": str(root / "tools"), "origin": "declared",
                                                   "file": f"tools/sources/{path.name}"}
                sources[f"tools/sources/{decl['name']}"] = layer.name
            for entry in entry_yaml.get("allow") or []:
                allow.append({"entry": str(entry), "source": layer.name})
        for tool_name, override in read_yaml(root / "tools" / "descriptions.yaml").items():
            if isinstance(override, Mapping):
                merged_override = {**descriptions.get(str(tool_name), {})}
                for key in ("title", "description"):
                    if override.get(key):
                        merged_override[key] = str(override[key])
                        merged_override[f"{key}_source"] = layer.name
                descriptions[str(tool_name)] = merged_override
        registered_dir = root / "tools" / "registered"
        if registered_dir.is_dir():
            from fabri.tools.registration import registered_sources

            for item in registered_sources(registered_dir):
                key = f"registered:{item['name']}"
                if not item["enabled"]:
                    tool_sources.pop(key, None)
                    continue
                tool_sources[key] = {"decl": item["decl"], "source": layer.name,
                                     "base_dir": str(root / "tools"), "origin": "registered",
                                     "version": item.get("version")}
                for entry in item["allow"]:
                    allow.append({"entry": entry, "source": layer.name})
                sources[f"tools/registered/{item['name']}"] = layer.name

    for name, flag in (setup.get("agent_types") or {}).items():
        if flag is False and name in agents:
            agents[name]["enabled"] = False

    return ResolvedSetup(
        layers=layers, setup=setup, agents=agents, skills=skills, context=context, data=data,
        plays=plays, tools={"sources": list(tool_sources.values()), "allow": allow, "descriptions": descriptions},
        sources=sources, content_hash=digest, warnings=warnings,
    )


_cache: dict[tuple, tuple[str, ResolvedSetup]] = {}
_cache_lock = threading.Lock()


def _cache_key(layers: list[Layer]) -> tuple:
    return tuple((layer.name, str(layer.path.resolve())) for layer in layers)


def resolve_setup(layers: Iterable[LayerLike], *, use_cache: bool = True) -> ResolvedSetup:
    """Resolve the layers, deepest last. The result is cached by a content
    hash of every layer file, so a change on disk is visible on the next
    call with no restart, and an unchanged tree costs one hash pass."""
    layer_list = as_layers(layers)
    digest = content_hash(layer_list)
    key = _cache_key(layer_list)
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and hit[0] == digest:
            return hit[1]
    resolved = _resolve_uncached(layer_list, digest)
    with _cache_lock:
        _cache[key] = (digest, resolved)
        while len(_cache) > 64:
            _cache.pop(next(iter(_cache)))
    return resolved


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
    from fabri.setups import toolset

    toolset.clear_cache()


__all__ = ["ResolvedSetup", "clear_cache", "is_hidden", "resolve_setup"]
