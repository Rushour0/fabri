"""Runtime writes to one layer: put, delete and disable, with validation.

Kinds and names:

=========  ==========================================  ===========================
kind       name                                        content
=========  ==========================================  ===========================
setup      ``setup.yaml`` (or empty)                   YAML text
agents     ``<type>`` or ``<type>/<file>``             {file: text} or text
skills     ``<name>`` or ``<name>/<file>``             {file: text} or text
plays      ``<name>`` or ``<name>/play.yaml``          {file: text} or text
context    ``<name>``                                  markdown text
data       ``<name>.<ext>``                            text or bytes
tools      ``tools.yaml`` (or empty)                   YAML text
=========  ==========================================  ===========================

A file write goes to a temp file and is renamed over the old one. A whole
item (``skills/<name>`` with a {file: text} mapping) is written to a temp
directory and swapped in, so a reader sees the old item or the new one. The
next resolve sees the change because the resolver keys its cache on file
content.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fabri.setups.layers import (
    SetupError,
    check_slug,
    layer_budgets,
    parse_yaml_text,
    read_yaml,
    split_frontmatter,
)
from fabri.style import slop_report
from fabri.tools.approval import ApprovalError
from fabri.tools.policy import PolicyError, ToolPolicy
from fabri.tools.sources import ToolSourceError, validate_source_decl

KINDS = ("setup", "agents", "skills", "plays", "context", "data", "tools")
DIR_FILES = {
    "agents": ("agent.yaml", "prompt.md", "prompt.append.md"),
    "skills": ("skill.yaml", "prompt.md"),
    "plays": ("play.yaml",),
}
MAIN_FILE = {"agents": "agent.yaml", "skills": "skill.yaml", "plays": "play.yaml"}
AGENT_KEYS = frozenset({"enabled", "max_steps", "max_cost_usd", "tools", "skills", "replace", "route",
                        "llm", "title", "description"})
SKILL_KEYS = frozenset({"name", "description", "when_to_use", "version", "types", "tools", "enabled", "title",
                        "tags", "origin", "load"})
SKILL_LOAD_MODES = ("always", "on_demand")
REFERENCE_FILE = re.compile(r"^references/[a-z0-9][a-z0-9._-]*\.(md|txt|json|ya?ml|csv)$")
HISTORY_FILE = ".fabri-revisions.json"
PLAY_KEYS_REQUIRED = ("name", "description")
TOOLS_KEYS = frozenset({"sources", "allow"})
_DATA_NAME = re.compile(r"^([a-z0-9][a-z0-9-]*)\.([a-z0-9]{1,8})$")
_GLOB = re.compile(r"[*?\[]")

Validator = Callable[[str, Any], None]


def allowed_file(kind: str, file: str) -> bool:
    """Whether `file` may sit inside a directory item of this kind. Skills
    also take `references/<name>.<ext>` files, one level deep."""
    if file in DIR_FILES.get(kind, ()):
        return True
    return kind == "skills" and bool(REFERENCE_FILE.fullmatch(file))


@dataclass
class PutResult:
    path: Path
    warnings: list[str] = field(default_factory=list)


def _slop_warnings(text: str, where: str) -> list[str]:
    report = slop_report(text)
    if not report.total:
        return []
    detail = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items()))
    return [f"{where}: writing tells found ({detail})"]


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SetupError(f"{where} must be a list of strings")
    return value


def _check_tool_names(names: list[str], policy: ToolPolicy | None, where: str) -> None:
    if policy is None or policy.allow is None:
        return
    for name in names:
        if _GLOB.search(name):
            continue
        if not policy.name_allowed(name):
            raise SetupError(f"{where}: {name!r} is not in the global allowlist")


# ---------------------------------------------------------------- validators


def validate_agent(name: str, files: Mapping[str, str], budgets: Mapping[str, int],
                   policy: ToolPolicy | None = None) -> list[str]:
    warnings: list[str] = []
    if "agent.yaml" in files:
        data = parse_yaml_text(files["agent.yaml"], f"agents/{name}/agent.yaml")
        unknown = set(data) - AGENT_KEYS
        if unknown:
            warnings.append(f"agents/{name}/agent.yaml: unknown keys {sorted(unknown)}")
        tools = _str_list(data.get("tools"), f"agents/{name} tools")
        _str_list(data.get("skills"), f"agents/{name} skills")
        replace = _str_list(data.get("replace"), f"agents/{name} replace")
        if set(replace) - {"tools", "skills"}:
            raise SetupError(f"agents/{name}: replace may only name tools and skills")
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise SetupError(f"agents/{name}: enabled must be true or false")
        route = data.get("route")
        if route is not None:
            if not isinstance(route, Mapping) or not isinstance(route.get("description"), str) \
                    or not isinstance(route.get("not_for"), str):
                raise SetupError(f"agents/{name}: route needs description and not_for strings")
        _check_tool_names(tools, policy, f"agents/{name} tools")
    for file in ("prompt.md", "prompt.append.md"):
        if file in files:
            text = files[file]
            if len(text) > budgets["prompt_chars"]:
                raise SetupError(f"agents/{name}/{file}: {len(text)} chars is over the {budgets['prompt_chars']} budget")
            warnings += _slop_warnings(text, f"agents/{name}/{file}")
    return warnings


def validate_skill(name: str, files: Mapping[str, str], budgets: Mapping[str, int],
                   policy: ToolPolicy | None = None, require_manifest: bool = True) -> list[str]:
    warnings: list[str] = []
    on_demand = False
    if "skill.yaml" in files:
        data = parse_yaml_text(files["skill.yaml"], f"skills/{name}/skill.yaml")
        if data.get("name") != name:
            raise SetupError(f"skills/{name}/skill.yaml: name must match the directory ({name})")
        if not isinstance(data.get("description"), str) or not data["description"].strip():
            raise SetupError(f"skills/{name}/skill.yaml: description is required")
        unknown = set(data) - SKILL_KEYS
        if unknown:
            warnings.append(f"skills/{name}/skill.yaml: unknown keys {sorted(unknown)}")
        _str_list(data.get("types"), f"skills/{name} types")
        _str_list(data.get("tags"), f"skills/{name} tags")
        if "when_to_use" in data and not isinstance(data["when_to_use"], str):
            raise SetupError(f"skills/{name}: when_to_use must be text")
        if data.get("load") is not None and data["load"] not in SKILL_LOAD_MODES:
            raise SetupError(f"skills/{name}: load must be one of {', '.join(SKILL_LOAD_MODES)}")
        if len(data["description"]) > 1024:
            warnings.append(f"skills/{name}: description is {len(data['description'])} chars; "
                            "it is listed in the prompt for on-demand skills, keep it under 1024")
        on_demand = data.get("load") == "on_demand"
        tools = _str_list(data.get("tools"), f"skills/{name} tools")
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise SetupError(f"skills/{name}: enabled must be true or false")
        _check_tool_names(tools, policy, f"skills/{name} tools")
        if data.get("origin") and not data.get("types"):
            warnings.append(f"skills/{name}: imported with types: [], so it loads for every agent type")
    elif require_manifest:
        raise SetupError(f"skills/{name}: skill.yaml is required")
    if "prompt.md" in files:
        text = files["prompt.md"]
        limit = budgets["skill_ondemand_chars"] if on_demand else budgets["skill_prompt_chars"]
        key = "skill_ondemand_chars" if on_demand else "skill_prompt_chars"
        if len(text) > limit:
            hint = "" if on_demand else "; set `load: on_demand` in skill.yaml to allow a longer skill"
            raise SetupError(f"skills/{name}/prompt.md: {len(text)} chars is over the {limit} {key} budget{hint}")
        warnings += _slop_warnings(text, f"skills/{name}/prompt.md")
    for file, text in files.items():
        if file.startswith("references/"):
            if len(text) > budgets["skill_ondemand_chars"]:
                raise SetupError(f"skills/{name}/{file}: {len(text)} chars is over the "
                                 f"{budgets['skill_ondemand_chars']} skill_ondemand_chars budget")
            if file.endswith(".md"):
                warnings += _slop_warnings(text, f"skills/{name}/{file}")
    return warnings


def validate_play(name: str, files: Mapping[str, str]) -> list[str]:
    data = parse_yaml_text(files.get("play.yaml", ""), f"plays/{name}/play.yaml")
    if data.get("name") != name:
        raise SetupError(f"plays/{name}/play.yaml: name must match the directory ({name})")
    if not isinstance(data.get("description"), str):
        raise SetupError(f"plays/{name}/play.yaml: description is required")
    return []


def validate_context(name: str, text: str, budgets: Mapping[str, int]) -> list[str]:
    meta, body = split_frontmatter(text)
    _str_list(meta.get("types"), f"context/{name} types")
    if len(body) > budgets["context_chars"]:
        raise SetupError(f"context/{name}: {len(body)} chars is over the {budgets['context_chars']} budget")
    return _slop_warnings(body, f"context/{name}")


def validate_data(name: str, content: bytes, budgets: Mapping[str, int]) -> list[str]:
    if len(content) > budgets["data_bytes"]:
        raise SetupError(f"data/{name}: {len(content)} bytes is over the {budgets['data_bytes']} budget")
    if name.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as exc:
            raise SetupError(f"data/{name}: invalid JSON ({exc})") from None
    return []


def validate_tools_yaml(text: str, policy: ToolPolicy | None = None) -> list[str]:
    data = parse_yaml_text(text, "tools/tools.yaml")
    warnings = [f"tools/tools.yaml: unknown keys {sorted(set(data) - TOOLS_KEYS)}"] if set(data) - TOOLS_KEYS else []
    sources = data.get("sources") or []
    if not isinstance(sources, list):
        raise SetupError("tools/tools.yaml: sources must be a list")
    names = set()
    for decl in sources:
        try:
            warnings += validate_source_decl(decl, policy)
        except (ToolSourceError, PolicyError, ApprovalError) as exc:
            raise SetupError(f"tools/tools.yaml: {exc}") from None
        if decl["name"] in names:
            raise SetupError(f"tools/tools.yaml: source {decl['name']} is declared twice")
        names.add(decl["name"])
    allow = _str_list(data.get("allow"), "tools/tools.yaml allow")
    _check_tool_names(allow, policy, "tools/tools.yaml allow")
    if policy is not None and policy.allow is not None:
        for entry in allow:
            if _GLOB.search(entry) and not any(fnmatch.fnmatchcase(entry, p) or fnmatch.fnmatchcase(p, entry)
                                               for p in policy.allow):
                warnings.append(f"tools/tools.yaml allow: {entry!r} matches nothing the global allowlist allows")
    return warnings


def validate_setup_yaml(text: str) -> list[str]:
    data = parse_yaml_text(text, "setup.yaml")
    if data.get("skill_load") not in (None, "always", "on_demand"):
        raise SetupError("setup.yaml: skill_load must be always or on_demand")
    budgets = data.get("budgets")
    if budgets is not None:
        if not isinstance(budgets, Mapping):
            raise SetupError("setup.yaml: budgets must be a mapping")
        from fabri.setups.layers import DEFAULT_BUDGETS

        for key, value in budgets.items():
            if key not in DEFAULT_BUDGETS:
                raise SetupError(f"setup.yaml: unknown budget {key!r} (known: {sorted(DEFAULT_BUDGETS)})")
            if not isinstance(value, int) or value <= 0:
                raise SetupError(f"setup.yaml: budget {key} must be a positive integer")
    return []


# ---------------------------------------------------------------- paths


def _item_target(layer: Path, kind: str, name: str) -> tuple[Path, str | None]:
    """(path, file) where file is None for a whole directory item."""
    if kind not in KINDS:
        raise SetupError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "setup":
        if name not in ("", "setup.yaml"):
            raise SetupError("setup items are named setup.yaml")
        return layer / "setup.yaml", "setup.yaml"
    if kind == "tools":
        if name not in ("", "tools.yaml"):
            raise SetupError("tools items are named tools.yaml (use fabri.tools.registration for single tools)")
        return layer / "tools" / "tools.yaml", "tools.yaml"
    if kind == "context":
        check_slug(name)
        return layer / "context" / f"{name}.md", f"{name}.md"
    if kind == "data":
        if not _DATA_NAME.fullmatch(name):
            raise SetupError("data items are named <slug>.<ext>")
        return layer / "data" / name, name
    item, slash, file = name.partition("/")
    check_slug(item)
    if not slash:
        return layer / kind / item, None
    if not allowed_file(kind, file):
        extra = "; skills also take references/<name>.md|txt|json|yaml|csv" if kind == "skills" else ""
        raise SetupError(f"{kind} files are {', '.join(DIR_FILES[kind])}{extra}")
    return layer / kind / item / file, file


def _present_files(kind: str, directory: Path) -> list[str]:
    """Relative names of the item files that exist in `directory`."""
    names = [f for f in DIR_FILES[kind] if (directory / f).is_file()]
    if kind == "skills" and (directory / "references").is_dir():
        names += [f"references/{p.name}" for p in sorted((directory / "references").iterdir())
                  if p.is_file() and REFERENCE_FILE.fullmatch(f"references/{p.name}")]
    return names


def _atomic_file(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _atomic_dir(path: Path, files: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        for file, text in files.items():
            (tmp / file).parent.mkdir(parents=True, exist_ok=True)
            (tmp / file).write_text(text, encoding="utf-8")
        if path.exists():
            old = path.parent / f".{path.name}.old-{uuid.uuid4().hex[:8]}"
            os.replace(path, old)
            os.replace(tmp, path)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(tmp, path)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ---------------------------------------------------------------- API


def validate_item(
    layer_dir: str | Path,
    kind: str,
    name: str,
    content: str | bytes | Mapping[str, str],
    *,
    policy: ToolPolicy | None = None,
    validators: Mapping[str, Validator] | None = None,
) -> list[str]:
    """Check an item as put_item would, without writing. Returns warnings;
    raises SetupError."""
    layer = Path(layer_dir)
    path, file = _item_target(layer, kind, name)
    budgets = layer_budgets(layer)
    warnings: list[str] = []
    if kind in DIR_FILES:
        item = name.partition("/")[0]
        if file is None:
            if isinstance(content, (str, bytes)):
                content = {MAIN_FILE[kind]: content.decode() if isinstance(content, bytes) else content}
            if not isinstance(content, Mapping) or not content:
                raise SetupError(f"{kind}/{item}: content must be a mapping of file names to text")
            bad = {f for f in content if not allowed_file(kind, f)}
            if bad:
                raise SetupError(f"{kind}/{item}: unexpected files {sorted(bad)}")
            files = dict(content)
        else:
            if not isinstance(content, str):
                raise SetupError(f"{kind}/{name}: content must be text")
            files = {file: content}
            # validate against the files already on disk
            current = layer / kind / item
            for other in _present_files(kind, current):
                if other != file:
                    files.setdefault(other, (current / other).read_text(encoding="utf-8"))
        if kind == "agents":
            warnings += validate_agent(item, files, budgets, policy)
        elif kind == "skills":
            warnings += validate_skill(item, files, budgets, policy)
        else:
            warnings += validate_play(item, files)
        parsed: Any = files
    elif kind == "data":
        data = content.encode() if isinstance(content, str) else content
        if not isinstance(data, bytes):
            raise SetupError("data content must be text or bytes")
        warnings += validate_data(name, data, budgets)
        parsed = data
    else:
        if not isinstance(content, str):
            raise SetupError(f"{kind} content must be text")
        if kind == "context":
            warnings += validate_context(name, content, budgets)
        elif kind == "tools":
            warnings += validate_tools_yaml(content, policy)
        else:
            warnings += validate_setup_yaml(content)
        parsed = content
    if validators and kind in validators:
        try:
            validators[kind](name, parsed)
        except SetupError:
            raise
        except Exception as exc:  # noqa: BLE001 -- host validators raise their own types
            raise SetupError(f"{kind}/{name}: {exc}") from None
    return warnings


def put_item(
    layer_dir: str | Path,
    kind: str,
    name: str,
    content: str | bytes | Mapping[str, str],
    *,
    policy: ToolPolicy | None = None,
    validators: Mapping[str, Validator] | None = None,
    actor: str | None = None,
) -> PutResult:
    """Validate, then write one item atomically. Clears a disabled marker
    for the item. `validators` maps a kind to a host check
    ``fn(name, parsed)`` that raises to refuse the write. A skill write that
    changes its files bumps the skill's revision (see `item_history`);
    `actor` is recorded with it."""
    layer = Path(layer_dir)
    warnings = validate_item(layer, kind, name, content, policy=policy, validators=validators)
    path, file = _item_target(layer, kind, name)
    if kind in DIR_FILES and file is None:
        if isinstance(content, (str, bytes)):
            content = {MAIN_FILE[kind]: content.decode() if isinstance(content, bytes) else content}
        _atomic_dir(path, dict(content))  # type: ignore[arg-type]
    else:
        _atomic_file(path, content)  # type: ignore[arg-type]
    marker = _marker(layer, kind, name)
    if marker is not None:
        marker.unlink(missing_ok=True)
    if kind == "skills":
        _record_revision(layer, name.partition("/")[0], actor)
    return PutResult(path, warnings)


def _skill_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for rel in sorted(_present_files("skills", directory)):
        digest.update(rel.encode() + b"\0" + (directory / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def _read_history(layer: Path) -> dict[str, Any]:
    try:
        data = json.loads((layer / HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_revision(layer: Path, item: str, actor: str | None, *, deleted: bool = False) -> None:
    """Append a revision to the layer's hidden `.fabri-revisions.json` when the
    skill's files differ from the last recorded state. The file is hidden, so
    it never changes the resolver's content hash. History keeps the last 50
    entries as {revision, sha, at, actor, deleted}; the full text is not kept."""
    history = _read_history(layer)
    key = f"skills/{item}"
    entry = history.get(key) or {"revision": 0, "sha": None, "history": []}
    sha = None if deleted else _skill_digest(layer / "skills" / item)
    if entry.get("sha") == sha:
        return
    entry["revision"] = int(entry.get("revision") or 0) + 1
    entry["sha"] = sha
    entry["history"] = [*(entry.get("history") or []), {
        "revision": entry["revision"], "sha": sha, "actor": actor, "deleted": deleted,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}][-50:]
    history[key] = entry
    _atomic_file(layer / HISTORY_FILE, json.dumps(history, indent=1, sort_keys=True))


def item_history(layer_dir: str | Path, kind: str, name: str) -> list[dict[str, Any]]:
    """Recorded revisions of a skill in this layer, oldest first. Empty for
    other kinds and for skills never written through put_item."""
    if kind != "skills":
        return []
    entry = _read_history(Path(layer_dir)).get(f"skills/{name}") or {}
    return list(entry.get("history") or [])


def item_revision(layer_dir: str | Path, kind: str, name: str) -> int:
    """Current revision of a skill in this layer, 0 when none is recorded."""
    if kind != "skills":
        return 0
    return int((_read_history(Path(layer_dir)).get(f"skills/{name}") or {}).get("revision") or 0)


def delete_item(layer_dir: str | Path, kind: str, name: str, *, actor: str | None = None) -> bool:
    """Remove an item (or one file of a directory item) from this layer.
    Returns False when nothing was there. A shallower layer's copy shows
    through again; use set_disabled to hide it."""
    path, file = _item_target(Path(layer_dir), kind, name)
    if not path.exists():
        return False
    if path.is_dir():
        old = path.parent / f".{path.name}.del-{uuid.uuid4().hex[:8]}"
        os.replace(path, old)
        shutil.rmtree(old, ignore_errors=True)
    else:
        path.unlink()
    if kind == "skills":
        item = name.partition("/")[0]
        _record_revision(Path(layer_dir), item, actor, deleted=not (Path(layer_dir) / "skills" / item).is_dir())
    return True


def _marker(layer: Path, kind: str, name: str) -> Path | None:
    if kind in ("setup", "tools"):
        return None
    item = name.partition("/")[0]
    if kind == "data":
        match = _DATA_NAME.fullmatch(name)
        item = match.group(1) if match else check_slug(name)
    check_slug(item)
    return layer / kind / f"{item}.disabled"


def set_disabled(layer_dir: str | Path, kind: str, name: str, disabled: bool = True) -> Path:
    """Write (or remove) ``<kind>/<name>.disabled`` in this layer, which
    hides the item from shallower layers too."""
    marker = _marker(Path(layer_dir), kind, name)
    if marker is None:
        raise SetupError(f"{kind} items cannot be disabled with a marker")
    if disabled:
        _atomic_file(marker, "")
    else:
        marker.unlink(missing_ok=True)
    return marker


def read_item(layer_dir: str | Path, kind: str, name: str) -> dict[str, str] | str | bytes | None:
    path, file = _item_target(Path(layer_dir), kind, name)
    if not path.exists():
        return None
    if path.is_dir():
        return {f: (path / f).read_text(encoding="utf-8") for f in _present_files(kind, path)}
    return path.read_bytes() if kind == "data" else path.read_text(encoding="utf-8")


__all__ = [
    "KINDS", "PutResult", "allowed_file", "delete_item", "item_history", "item_revision", "put_item", "read_item",
    "read_yaml", "set_disabled", "validate_item",
]
