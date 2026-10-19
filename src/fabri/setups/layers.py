"""Layer basics shared by the resolver, the store, import and lint."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

DEFAULT_BUDGETS: dict[str, int] = {
    "skill_prompt_chars": 4000,  # one always-loaded skill's prompt.md
    "skill_ondemand_chars": 40000,  # one on-demand skill's prompt.md, and each references/ file
    "context_chars": 6000,  # all context for one agent type after merge
    "prompt_chars": 12000,  # one agent type's merged prompt
    "data_bytes": 1_048_576,  # one data file
}


class SetupError(ValueError):
    """A layer item is invalid or a write was refused."""


@dataclass(frozen=True)
class Layer:
    """One directory in the ordered layer list. `name` is what every
    resolved item records as its source."""

    name: str
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


LayerLike = Layer | tuple[str, str | Path] | str | Path


def as_layer(item: LayerLike) -> Layer:
    if isinstance(item, Layer):
        return item
    if isinstance(item, tuple):
        return Layer(str(item[0]), Path(item[1]))
    text = str(item)
    if "=" in text and not Path(text).exists():
        name, _, path = text.partition("=")
        return Layer(name, Path(path))
    path = Path(text)
    return Layer(path.resolve().name or text, path)


def as_layers(items: Iterable[LayerLike]) -> list[Layer]:
    layers = [as_layer(i) for i in items]
    names = [layer.name for layer in layers]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SetupError(f"layer names must be unique (repeated: {', '.join(dupes)})")
    return layers


def check_slug(value: str, what: str = "name") -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise SetupError(f"{what} {value!r} must be a slug ([a-z0-9][a-z0-9-]*)")
    return value


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SetupError(f"{path}: invalid YAML ({exc})") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SetupError(f"{path}: must be a mapping")
    return data


def parse_yaml_text(text: str, what: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SetupError(f"{what}: invalid YAML ({exc})") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SetupError(f"{what}: must be a mapping")
    return data


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---\\n<yaml>\\n---\\n<body>``. Text without frontmatter returns
    ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            front = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            try:
                meta = yaml.safe_load(front) or {}
            except yaml.YAMLError as exc:
                raise SetupError(f"invalid frontmatter ({exc})") from exc
            return (meta if isinstance(meta, dict) else {}), body
    return {}, text


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def is_hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def layer_files(layer: Layer) -> list[Path]:
    if not layer.path.is_dir():
        return []
    return sorted(p for p in layer.path.rglob("*") if p.is_file() and not is_hidden(p.relative_to(layer.path)))


def content_hash(layers: Iterable[Layer]) -> str:
    """sha256 over every non-hidden file in every layer (name, relative path
    and bytes). Hidden files and directories (``.name``) hold state such as
    sync snapshots and never change the hash."""
    digest = hashlib.sha256()
    for layer in layers:
        digest.update(f"\0layer\0{layer.name}\0{layer.path.resolve()}\0".encode())
        for path in layer_files(layer):
            digest.update(str(path.relative_to(layer.path)).encode() + b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
            digest.update(b"\0")
    return digest.hexdigest()


def layer_budgets(layer_path: Path, setup_override: Mapping[str, Any] | None = None) -> dict[str, int]:
    setup = setup_override if setup_override is not None else read_yaml(Path(layer_path) / "setup.yaml")
    raw = setup.get("budgets") or {}
    out = dict(DEFAULT_BUDGETS)
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if key in DEFAULT_BUDGETS and isinstance(value, int) and value > 0:
                out[key] = value
    return out
