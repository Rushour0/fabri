"""Import skills from another layout into a layer.

Formats:

- ``skill-md``: one directory per skill holding ``SKILL.md`` (YAML
  frontmatter with ``name`` and ``description``, then a markdown body). The
  slug comes from the directory name (``chat_canvas`` -> ``chat-canvas``),
  the frontmatter name is kept as ``title``, ``tags`` are kept, and the body
  becomes ``prompt.md``. A frontmatter ``types`` list is kept; otherwise
  ``types: []`` (all agent types) and lint warns about it. ``when_to_use`` is
  kept and ``references/`` files are copied. A body over the always-loaded
  budget is imported with ``load: on_demand``.
- ``fabri``: fabri's own skill layout (``skill.yaml`` with name, description,
  version; ``prompts/*.md`` joined into ``prompt.md``; ``config.yaml``
  ``tools.enabled`` mapped to ``tools``). Executable tools under ``tools/``
  are not imported, with a warning.

Import is idempotent. Each skill is reported as created, updated, skipped
(identical content already there) or invalid (with the reason; not written).
Budgets come from the target layer's ``setup.yaml``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from fabri.setups.layers import SetupError, layer_budgets, read_yaml, split_frontmatter
from fabri.setups.store import put_item, read_item, validate_item
from fabri.tools.policy import ToolPolicy

FORMATS = ("skill-md", "fabri")


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    invalid: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped,
                "invalid": self.invalid, "warnings": self.warnings, "dry_run": self.dry_run}


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _from_skill_md(directory: Path) -> tuple[str, dict[str, str], list[str]]:
    text = (directory / "SKILL.md").read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    slug = slugify(directory.name)
    manifest: dict[str, Any] = {"name": slug, "description": str(meta.get("description") or "").strip()}
    if meta.get("name"):
        manifest["title"] = str(meta["name"])
    manifest["version"] = str(meta.get("version") or 1)
    types = meta.get("types") or meta.get("routes") or []
    manifest["types"] = [str(t) for t in types] if isinstance(types, list) else []
    if isinstance(meta.get("tags"), list):
        manifest["tags"] = [str(t) for t in meta["tags"]]
    if isinstance(meta.get("tools"), list):
        manifest["tools"] = [str(t) for t in meta["tools"]]
    if meta.get("when_to_use"):
        manifest["when_to_use"] = str(meta["when_to_use"]).strip()
    manifest["origin"] = "skill-md"
    files = {"skill.yaml": _dump(manifest), "prompt.md": body.strip() + "\n"}
    refs = directory / "references"
    if refs.is_dir():
        for path in sorted(refs.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                files[f"references/{path.name}"] = path.read_text(encoding="utf-8")
    return slug, files, []


def _from_fabri(directory: Path) -> tuple[str, dict[str, str], list[str]]:
    meta = read_yaml(directory / "skill.yaml")
    slug = slugify(str(meta.get("name") or directory.name))
    warnings: list[str] = []
    manifest: dict[str, Any] = {"name": slug, "description": str(meta.get("description") or "").strip(),
                                "version": str(meta.get("version") or 1)}
    if meta.get("name") and meta["name"] != slug:
        manifest["title"] = str(meta["name"])
    manifest["types"] = [str(t) for t in meta.get("types") or []]
    config = read_yaml(directory / "config.yaml")
    enabled = ((config.get("tools") or {}) if isinstance(config.get("tools"), dict) else {}).get("enabled")
    if isinstance(enabled, list):
        manifest["tools"] = [str(t) for t in enabled]
    manifest["origin"] = "fabri"
    prompts = sorted((directory / "prompts").glob("*.md")) if (directory / "prompts").is_dir() else []
    body = "\n\n".join(p.read_text(encoding="utf-8").strip() for p in prompts)
    if (directory / "tools").is_dir() and any((directory / "tools").iterdir()):
        warnings.append(f"{slug}: executable tools under tools/ were not imported")
    files = {"skill.yaml": _dump(manifest)}
    if body:
        files["prompt.md"] = body + "\n"
    return slug, files, warnings


def _candidates(source: Path, fmt: str) -> list[Path]:
    marker = "SKILL.md" if fmt == "skill-md" else "skill.yaml"
    if (source / marker).is_file():
        return [source]
    return sorted(p for p in source.iterdir() if p.is_dir() and (p / marker).is_file())


def import_skills(
    source: str | Path,
    into: str | Path,
    *,
    fmt: str = "skill-md",
    dry_run: bool = False,
    policy: ToolPolicy | None = None,
) -> ImportReport:
    if fmt not in FORMATS:
        raise SetupError(f"format must be one of {', '.join(FORMATS)}")
    source, into = Path(source), Path(into)
    if not source.is_dir():
        raise SetupError(f"{source} is not a directory")
    report = ImportReport(dry_run=dry_run)
    reader = _from_skill_md if fmt == "skill-md" else _from_fabri
    seen: set[str] = set()
    for directory in _candidates(source, fmt):
        try:
            slug, files, warnings = reader(directory)
        except (OSError, SetupError, yaml.YAMLError) as exc:
            report.invalid.append({"name": directory.name, "reason": str(exc)})
            continue
        if slug in seen:
            report.invalid.append({"name": slug, "reason": f"two source skills map to the slug {slug}"})
            continue
        seen.add(slug)
        report.warnings += warnings
        if len(files.get("prompt.md", "")) > layer_budgets(into)["skill_prompt_chars"] and "skill.yaml" in files:
            manifest = yaml.safe_load(files["skill.yaml"])
            if manifest.get("load") is None:
                manifest["load"] = "on_demand"
                files["skill.yaml"] = _dump(manifest)
                report.warnings.append(f"{slug}: prompt is over the always-loaded budget, imported with load: on_demand")
        try:
            report.warnings += validate_item(into, "skills", slug, files, policy=policy)
        except SetupError as exc:
            report.invalid.append({"name": slug, "reason": str(exc)})
            continue
        existing = read_item(into, "skills", slug)
        if existing == files:
            report.skipped.append(slug)
            continue
        if not dry_run:
            put_item(into, "skills", slug, files, policy=policy)
        (report.updated if existing is not None else report.created).append(slug)
    return report
