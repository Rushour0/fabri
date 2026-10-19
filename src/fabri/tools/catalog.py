"""A catalog entry per tool: title, an agent-grade description, params,
approval, probe options and examples.

The catalog is built from the resolved tool set and the state the host keeps
next to it (``tools/.fabri-state`` in the deepest layer by default):

- ``probe.json``: what fabri.tools.probe captured (examples, options, time)
- ``descriptions.json``: LLM rewrites, keyed by a hash of the op schema and
  its probe examples (see fabri.tools.describe)
- ``catalog.json``: the last catalog written by ``fabri tools sync``

Title and description come from, in order: a layer's
``tools/descriptions.yaml``, a cached LLM rewrite, then deterministic text
built from the schema. Each entry records which one won.

The catalog's title and description become the tool manifest's, so fabri's
tool retrieval (top-k by embedding) and the find_tools meta tool rank on them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_error, tool_ok

if TYPE_CHECKING:
    from fabri.setups.resolver import ResolvedSetup
    from fabri.setups.toolset import ResolvedTool, ResolvedToolSet
    from fabri.tools.registry import ToolRegistry

TITLE_CAP = 60
DESCRIPTION_CAP = 1200
FIND_TOOLS = "find_tools"
DESCRIBE_TOOL = "describe_tool"
META_TOOLS = (FIND_TOOLS, DESCRIBE_TOOL)

_ERROR_HINTS = {
    "400": "invalid input: fix the arguments",
    "401": "not authenticated: do not retry",
    "403": "not allowed: do not retry",
    "404": "not found: check the id",
    "409": "conflict: re-read the record and retry with current values",
    "422": "invalid input: fix the arguments",
    "429": "rate limited: wait before retrying",
}


@dataclass
class CatalogParam:
    name: str
    type: str
    required: bool
    enum: list | None = None
    options: list | None = None
    location: str | None = None


@dataclass
class CatalogEntry:
    name: str
    title: str
    description: str
    approval: str
    kind: str
    source: str
    layer: str
    origin: str
    alias: list[str] = field(default_factory=list)
    resource: str | None = None
    action: str | None = None
    method: str | None = None
    path: str | None = None
    tags: list[str] = field(default_factory=list)
    params: list[CatalogParam] = field(default_factory=list)
    examples: list[Any] = field(default_factory=list)
    options: dict[str, list] = field(default_factory=dict)
    probed_at: str | None = None
    title_source: str = "schema"
    description_source: str = "schema"
    schema_hash: str = ""

    def search_text(self) -> str:
        return f"{self.name} {' '.join(self.alias)}\n{self.title}\n{self.description}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CatalogEntry":
        params = [CatalogParam(**p) for p in data.get("params") or []]
        return cls(**{**data, "params": params})


@dataclass
class Catalog:
    entries: dict[str, CatalogEntry] = field(default_factory=dict)

    def get(self, name: str) -> CatalogEntry | None:
        if name in self.entries:
            return self.entries[name]
        for entry in self.entries.values():
            if name in entry.alias:
                return entry
        return None

    def to_json(self) -> dict[str, Any]:
        return {"version": 1, "entries": [e.to_dict() for _, e in sorted(self.entries.items())]}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Catalog":
        return cls({e["name"]: CatalogEntry.from_dict(e) for e in data.get("entries") or []})

    def search(self, query: str, k: int = 5, names: Iterable[str] | None = None) -> list[CatalogEntry]:
        """Rank entries for `query`. Uses fabri's embeddings when installed
        and falls back to word overlap otherwise."""
        pool = [e for e in self.entries.values() if names is None or e.name in set(names)]
        if not pool:
            return []
        scores = _embedding_scores(query, pool)
        if scores is None:
            scores = _lexical_scores(query, pool)
        ranked = sorted(zip(pool, scores), key=lambda pair: (-pair[1], pair[0].name))
        return [entry for entry, _ in ranked[: max(1, k)]]


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    out = []
    for word in _WORD.findall(text.lower().replace("_", " ")):
        out.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return out


def _lexical_scores(query: str, pool: list[CatalogEntry]) -> list[float]:
    docs = [_words(e.search_text()) for e in pool]
    q = set(_words(query))
    n = len(docs)
    df = {w: sum(1 for d in docs if w in d) for w in q}
    out = []
    for doc in docs:
        counts = {w: doc.count(w) for w in q}
        out.append(sum((1 + math.log(c)) * math.log(1 + n / df[w]) for w, c in counts.items() if c))
    return out


def _embedding_scores(query: str, pool: list[CatalogEntry]) -> list[float] | None:
    try:
        from fabri.memory.embeddings import embed, embeddings_available

        if not embeddings_available():
            return None
        q = embed(query)
        return [sum(a * b for a, b in zip(q, _entry_vector(e))) for e in pool]
    except Exception:  # noqa: BLE001 -- ranking falls back to word overlap
        return None


_vector_cache: dict[str, list[float]] = {}


def _entry_vector(entry: CatalogEntry) -> list[float]:
    from fabri.memory.embeddings import embed

    text = entry.search_text()
    key = hashlib.sha256(text.encode()).hexdigest()
    if key not in _vector_cache:
        _vector_cache[key] = embed(text)
    return _vector_cache[key]


# ---------------------------------------------------------------- text


def _human(resource: str) -> str:
    return resource.replace("_", " ").strip()


def _plural(words: str) -> str:
    if words.endswith("y") and not words.endswith(("ay", "ey", "oy", "uy")):
        return words[:-1] + "ies"
    if words.endswith(("s", "x", "z", "ch", "sh")):
        return words + "es"
    return words + "s"


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _cap(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip(" ,;:") + "..."


def deterministic_title(tool: "ResolvedTool") -> str:
    op = tool.operation
    if op is not None:
        if op.summary and len(op.summary) <= TITLE_CAP:
            return op.summary.rstrip(".")
        res = _human(op.resource)
        if op.action == "list":
            return _cap(f"List {_plural(res)}", TITLE_CAP)
        verb = op.action.replace("_", " ")
        return _cap(f"{verb[:1].upper()}{verb[1:]} {_article(res)} {res}", TITLE_CAP)
    if tool.manifest.title:
        return _cap(tool.manifest.title, TITLE_CAP)
    words = tool.name.replace("_", " ").replace("-", " ")
    return _cap(words[:1].upper() + words[1:], TITLE_CAP)


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return match.group(1) if match else text


def _params(tool: "ResolvedTool", options: Mapping[str, list]) -> list[CatalogParam]:
    schema = tool.manifest.input_schema or {}
    required = set(schema.get("required") or [])
    locations = {p.name: p.location for p in tool.operation.params} if tool.operation else {}
    out = []
    for name, prop in (schema.get("properties") or {}).items():
        prop = prop if isinstance(prop, Mapping) else {}
        enum = [v for v in prop.get("enum") or [] if v is not None] or None
        kind = prop.get("type")
        kind = "|".join(kind) if isinstance(kind, list) else (kind or ("oneOf" if "oneOf" in prop else "any"))
        out.append(CatalogParam(name, str(kind), name in required, enum, options.get(name),
                                locations.get(name) or ("body" if tool.operation else None)))
    out.sort(key=lambda p: (not p.required, p.name))
    return out


def deterministic_description(tool: "ResolvedTool", siblings: list["ResolvedTool"],
                              params: list[CatalogParam]) -> str:
    """What it does, when to use it, key params, errors and approval. Each
    part is one short line."""
    op = tool.operation
    lines: list[str] = []
    if op is not None:
        what = _first_sentence(op.text) or (op.summary.rstrip(".") + "." if op.summary else "")
        if op.summary and op.text and not op.text.startswith(op.summary):
            what = f"{op.summary.rstrip('.')}. {_first_sentence(op.text)}"
        lines.append(what or f"{deterministic_title(tool)}.")
        if siblings:
            refs = "; ".join(f"use {s.name} to {deterministic_title(s)[:1].lower()}{deterministic_title(s)[1:]}"
                             for s in siblings[:4])
            group = f" ({', '.join(op.tags)})" if op.tags else ""
            lines.append(f"Use it to {deterministic_title(tool).lower()}{group}. For other {_human(op.resource)} "
                         f"actions: {refs}.")
    else:
        lines.append(_first_sentence(tool.base_description or tool.manifest.description)
                     or f"{deterministic_title(tool)}.")
    if params:
        bits = []
        for p in params[:8]:
            bit = f"{p.name} ({p.type}{', required' if p.required else ''})"
            if p.enum:
                bit += f" one of {'|'.join(str(v) for v in p.enum[:8])}"
            elif p.options:
                bit += f" e.g. {', '.join(str(v) for v in p.options[:5])}"
            bits.append(bit)
        lines.append("Params: " + "; ".join(bits) + ".")
    if op is not None:
        errors = []
        for code, info in sorted(op.responses.items()):
            if code[:1] in ("2", "3"):
                continue
            reasons = info.get("reasons") or []
            hint = _ERROR_HINTS.get(code) or ("server error: retry once later" if code.startswith("5")
                                              else (info.get("description") or "error"))
            errors.append(f"{code}{' (' + '|'.join(reasons) + ')' if reasons else ''} {hint}")
        if errors:
            lines.append("Errors: " + "; ".join(errors) + ".")
    lines.append(f"Approval: {tool.approval}.")
    return _cap("\n".join(lines), DESCRIPTION_CAP)


# ---------------------------------------------------------------- state


def state_dir_for(setup: "ResolvedSetup", state_dir: str | Path | None = None) -> Path | None:
    if state_dir is not None:
        return Path(state_dir)
    from fabri.setups.toolset import default_state_dir

    return default_state_dir(setup)


def read_state(directory: Path | None, name: str) -> dict[str, Any]:
    if directory is None:
        return {}
    path = directory / name
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(directory: Path, name: str, data: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, default=str)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def describe_cache_key(tool: "ResolvedTool", probe: Mapping[str, Any] | None) -> str:
    base = tool.operation.schema_hash() if tool.operation else hashlib.sha256(json.dumps(
        {"d": tool.base_description or tool.manifest.description, "s": tool.manifest.input_schema}, sort_keys=True).encode()).hexdigest()[:16]
    examples = json.dumps((probe or {}).get("examples") or [], sort_keys=True, default=str)
    return f"{tool.name}:{hashlib.sha256((base + examples).encode()).hexdigest()[:16]}"


# ---------------------------------------------------------------- build


def build_catalog(toolset: "ResolvedToolSet", setup: "ResolvedSetup",
                  state_dir: str | Path | None = None) -> Catalog:
    directory = state_dir_for(setup, state_dir)
    probes = read_state(directory, "probe.json").get("tools") or {}
    llm_cache = read_state(directory, "descriptions.json").get("entries") or {}
    overrides = setup.tools.get("descriptions") or {}
    by_resource: dict[tuple[str, str], list] = {}
    for tool in toolset.tools.values():
        if tool.operation is not None:
            by_resource.setdefault((tool.source, tool.operation.resource), []).append(tool)

    catalog = Catalog()
    for tool in toolset.tools.values():
        probe = probes.get(tool.name) or {}
        options = dict(probe.get("options") or {})
        params = _params(tool, options)
        siblings = []
        if tool.operation is not None:
            siblings = [t for t in by_resource.get((tool.source, tool.operation.resource), []) if t is not tool]
        title, title_src = deterministic_title(tool), "schema"
        desc, desc_src = deterministic_description(tool, siblings, params), "schema"
        cached = llm_cache.get(describe_cache_key(tool, probe))
        if isinstance(cached, Mapping):
            if cached.get("title"):
                title, title_src = _cap(str(cached["title"]), TITLE_CAP), "llm"
            if cached.get("description"):
                desc, desc_src = _cap(str(cached["description"]), DESCRIPTION_CAP), "llm"
        override = overrides.get(tool.name) or next(
            (overrides[a] for a in tool.aliases if a in overrides), None)
        if override:
            if override.get("title"):
                title, title_src = _cap(override["title"], TITLE_CAP), f"layer:{override['title_source']}"
            if override.get("description"):
                desc, desc_src = _cap(override["description"], DESCRIPTION_CAP), f"layer:{override['description_source']}"
        op = tool.operation
        catalog.entries[tool.name] = CatalogEntry(
            name=tool.name, title=title, description=desc, approval=tool.approval, kind=tool.kind,
            source=tool.source, layer=tool.layer, origin=tool.origin, alias=tool.aliases,
            resource=op.resource if op else None, action=op.action if op else None,
            method=tool.method, path=tool.path, tags=list(op.tags) if op else [], params=params,
            examples=list(probe.get("examples") or []), options=options,
            probed_at=probe.get("probed_at"), title_source=title_src, description_source=desc_src,
            schema_hash=op.schema_hash() if op else "",
        )
    return catalog


def attach_catalog(toolset: "ResolvedToolSet", setup: "ResolvedSetup",
                   state_dir: str | Path | None = None) -> Catalog:
    """Build the catalog and copy each entry's title and description onto
    the tool's manifest, so retrieval and the model see the same text."""
    catalog = build_catalog(toolset, setup, state_dir)
    for name, entry in catalog.entries.items():
        manifest = toolset.tools[name].manifest
        manifest.title = entry.title
        manifest.description = entry.description
    toolset.catalog = catalog
    return catalog


def save_catalog(catalog: Catalog, directory: Path) -> Path:
    return write_state(directory, "catalog.json", catalog.to_json())


# ---------------------------------------------------------------- meta tools


def register_meta_tools(registry: "ToolRegistry", toolset: "ResolvedToolSet",
                        names: Iterable[str] | None = None) -> None:
    """Add find_tools and describe_tool to `registry`. Both only see the
    tools in `names` (default: everything in the registry). Each asks the
    agent loop to expose what it returns, so the model can call a found tool
    in the same run even when retrieval hid it."""
    catalog = toolset.catalog
    if catalog is None:
        raise ValueError("the tool set has no catalog; resolve it with catalog=True")
    visible = list(names) if names is not None else [n for n in registry.tools if n not in META_TOOLS]

    def find_tools(args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("find_tools: `query` is required")
        k = max(1, min(int(args.get("k") or 5), 20))
        hits = catalog.search(query, k, names=visible)
        registry.request_exposure([e.name for e in hits])
        return tool_ok({"tools": [{"name": e.name, "title": e.title,
                                   "summary": _first_sentence(e.description)} for e in hits]})

    def describe_tool(args: dict) -> dict:
        name = str(args.get("name") or "")
        entry = catalog.get(name)
        if entry is None or entry.name not in visible:
            return tool_error(f"describe_tool: no tool named {name!r}")
        registry.request_exposure([entry.name])
        manifest = registry.tools.get(entry.name)
        return tool_ok({
            "name": entry.name, "aliases": entry.alias, "title": entry.title,
            "description": entry.description, "approval": entry.approval,
            "method": entry.method, "path": entry.path,
            "input_schema": manifest.input_schema if manifest else {},
            "examples": entry.examples, "options": entry.options,
        })

    registry.register_callable(ToolManifest(
        name=FIND_TOOLS, title="Find tools",
        description="Search the available tools by what you want to do. Returns tool names with "
                    "one-line descriptions. Any returned tool can be called right after.",
        command=[], input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "What you want to do, in plain words."},
            "k": {"type": "integer", "description": "How many tools to return (default 5)."}},
            "required": ["query"]},
        output_schema={"type": "object"}, approval="read"), find_tools)
    registry.register_callable(ToolManifest(
        name=DESCRIBE_TOOL, title="Describe a tool",
        description="Full schema, examples and known options for one tool. The tool can be called right after.",
        command=[], input_schema={"type": "object", "properties": {"name": {"type": "string"}},
                                  "required": ["name"]},
        output_schema={"type": "object"}, approval="read"), describe_tool)
