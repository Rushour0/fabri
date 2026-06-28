"""B4 -- skills registry: install a reusable capability (prompt template + tool
manifests + a config snippet) into a project so it carries across products.

A *skill* is a directory on disk. Its layout is small and documented so a skill
is portable, reviewable, and version-controllable:

    <skill>/
      skill.yaml          # required: {name, description, version}
      config.yaml         # optional: a config snippet merged into agent.yaml
      prompts/            # optional: .md prompt templates -> project prompts/
        <name>.md
      tools/              # optional: manifest+executable pairs -> tools/agent_tools/
        <tool>.json
        <tool>.<ext>

Install is the inverse of authoring, in three additive moves:

- ``tools/*`` copy into the project's ``tools/agent_tools/`` (the same dir the
  B2 tool-writer and the B1 ideator scaffold into),
- ``prompts/*`` copy into the project's ``prompts/`` dir,
- ``config.yaml`` is MERGED into the project's ``agent.yaml`` with project values
  winning -- existing keys are never clobbered, lists are unioned (so a skill's
  ``tools.enabled`` entries are *added*, not replaced), and any scalar the skill
  would have overwritten is reported as a conflict rather than applied.

Install is idempotent: a second run copies nothing new (existing files are left
untouched unless ``force``) and the config merge is a no-op (its keys / list
items are already present). No LLM, no network -- pure file work.

Three operations, surfaced under the CLI's ``skills`` subcommand group:
:func:`discover_skills` (``fabri skills list``), :func:`install_skill`
(``fabri skills install``), and :func:`new_skill` (``fabri skills add``, which
scaffolds a fresh skill skeleton to author).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from fabri.config import ConfigError, _deep_merge
from fabri.core import structured
from fabri.core.logging_setup import get_logger

logger = get_logger()

# Bundled example skills ship with the wheel (see pyproject package-data) so a
# fresh install can `fabri skills install <example>` with nothing else on disk.
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills_examples"

# Default project-local skills dir, relative to where the user runs fabri.
DEFAULT_PROJECT_SKILLS_DIR = "skills"

# Where a skill's parts land inside a project, mirroring the tool-writer / ideator.
PROJECT_TOOLS_DIR = "tools/agent_tools"
PROJECT_PROMPTS_DIR = "prompts"

SKILL_MANIFEST_NAME = "skill.yaml"
SKILL_CONFIG_NAME = "config.yaml"

# The shape skill.yaml must satisfy, in the JSON-Schema subset `structured`
# validates -- the one validator we ship, so the skill format and the agent
# loop never drift on what "valid" means.
SKILL_MANIFEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "string"},
    },
    "required": ["name", "description", "version"],
}


class SkillError(ValueError):
    """A skill could not be read or installed: a missing/malformed ``skill.yaml``,
    an unresolvable skill name, or a target that already holds a project. The CLI
    catches this and prints a clean message + exit 1 rather than a raw traceback."""


@dataclass(frozen=True)
class Skill:
    """A loaded skill bundle. `path` is the skill directory; the prompt/tool/
    config accessors derive their locations from it, returning None when the
    optional part is absent."""

    name: str
    description: str
    version: str
    path: Path

    @property
    def config_path(self) -> Path | None:
        p = self.path / SKILL_CONFIG_NAME
        return p if p.is_file() else None

    @property
    def prompts_dir(self) -> Path | None:
        p = self.path / "prompts"
        return p if p.is_dir() else None

    @property
    def tools_dir(self) -> Path | None:
        p = self.path / "tools"
        return p if p.is_dir() else None


# ---------------------------------------------------------------------------
# load / discover
# ---------------------------------------------------------------------------


def load_skill(path: str | Path) -> Skill:
    """Load and validate the skill at `path` (a directory holding `skill.yaml`).

    Raises :class:`SkillError` if the directory or its manifest is missing, the
    manifest is malformed YAML, or it doesn't match :data:`SKILL_MANIFEST_SCHEMA`.
    """
    directory = Path(path)
    manifest_path = directory / SKILL_MANIFEST_NAME
    if not manifest_path.is_file():
        raise SkillError(f"not a skill: no {SKILL_MANIFEST_NAME} in {directory}")
    try:
        data = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"malformed YAML in {manifest_path}: {e}") from e
    if not isinstance(data, dict):
        raise SkillError(f"{manifest_path} must be a mapping")
    errors = structured.validate(data, SKILL_MANIFEST_SCHEMA)
    if errors:
        raise SkillError(f"{manifest_path} is invalid: {'; '.join(errors)}")
    return Skill(
        name=data["name"],
        description=data["description"],
        version=data["version"],
        path=directory,
    )


def _scan_dir(directory: Path) -> list[Skill]:
    """Load every immediate subdirectory of `directory` that holds a skill.yaml.
    Subdirectories without a manifest, or with an invalid one, are skipped with a
    warning so one bad skill never breaks discovery."""
    skills: list[Skill] = []
    if not directory.is_dir():
        return skills
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / SKILL_MANIFEST_NAME).is_file():
            continue
        try:
            skills.append(load_skill(child))
        except SkillError as e:
            logger.warning("skills: skipping %s (%s)", child, e)
    return skills


def discover_skills(
    skills_dir: str | Path | None = None, *, include_bundled: bool = True
) -> list[Skill]:
    """Discover installable skills: the bundled examples plus a project-local
    `skills_dir` (default ``./skills``).

    Bundled examples come first; a project skill that shadows a bundled one by
    name is kept too (both are listed) so the user can see the choice. Pass
    ``include_bundled=False`` to list only project skills.
    """
    skills: list[Skill] = []
    if include_bundled:
        skills.extend(_scan_dir(BUNDLED_SKILLS_DIR))
    project_dir = Path(skills_dir) if skills_dir is not None else Path(DEFAULT_PROJECT_SKILLS_DIR)
    skills.extend(_scan_dir(project_dir))
    return skills


def resolve_skill(ref: str, skills_dir: str | Path | None = None) -> Skill:
    """Resolve a skill reference to a loaded :class:`Skill`.

    `ref` is either a path to a skill directory or a bare skill name. A name is
    looked up first in the project `skills_dir` (default ``./skills``), then in
    the bundled examples, so a project skill shadows a bundled one of the same
    name. Raises :class:`SkillError` when nothing matches.
    """
    candidate = Path(ref)
    if candidate.is_dir() and (candidate / SKILL_MANIFEST_NAME).is_file():
        return load_skill(candidate)

    project_dir = Path(skills_dir) if skills_dir is not None else Path(DEFAULT_PROJECT_SKILLS_DIR)
    for base in (project_dir, BUNDLED_SKILLS_DIR):
        match = base / ref
        if match.is_dir() and (match / SKILL_MANIFEST_NAME).is_file():
            return load_skill(match)

    available = ", ".join(s.name for s in discover_skills(skills_dir)) or "(none)"
    raise SkillError(
        f"no skill named {ref!r} (and not a skill directory). Available: {available}"
    )


def render_skills_listing(skills: list[Skill]) -> str:
    """Human-readable listing of discovered skills as ``name (version) -- desc``."""
    if not skills:
        return "no skills found (looked in the bundled examples and ./skills)."
    width = max(len(s.name) for s in skills)
    lines = [f"{len(skills)} skill(s) available:"]
    for s in skills:
        first = (s.description or "").splitlines()[0] if s.description else ""
        lines.append(f"  {s.name.ljust(width)}  ({s.version})  {first}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# config merge: additive, project-wins, list-union, conflict-reporting
# ---------------------------------------------------------------------------


def _resolve_overlay(project: dict, skill: dict, path: str = "") -> tuple[dict, list[str]]:
    """Build the safe overlay to deep-merge over `project` so the merge is purely
    additive, and collect the conflicts that resolution suppressed.

    Returns ``(overlay, conflicts)``. The overlay carries:
      - keys present only in the skill (brought in verbatim),
      - per-key recursion for nested mappings,
      - the *union* of list values (project order preserved, new skill items
        appended) so a skill's ``tools.enabled`` entries are added, not lost.
    A scalar the skill would change, or a type mismatch (skill mapping vs project
    scalar, etc.), is dropped from the overlay and reported as a conflict so the
    project's value always survives.
    """
    overlay: dict = {}
    conflicts: list[str] = []
    for key, sval in skill.items():
        here = f"{path}.{key}" if path else key
        if key not in project:
            overlay[key] = sval
            continue
        pval = project[key]
        if isinstance(pval, dict) and isinstance(sval, dict):
            sub_overlay, sub_conflicts = _resolve_overlay(pval, sval, here)
            if sub_overlay:
                overlay[key] = sub_overlay
            conflicts.extend(sub_conflicts)
        elif isinstance(pval, list) and isinstance(sval, list):
            additions = [item for item in sval if item not in pval]
            if additions:
                overlay[key] = pval + additions
        elif pval == sval:
            continue  # already present and identical -- idempotent no-op
        else:
            conflicts.append(
                f"{here}: kept project value {pval!r} (skill wanted {sval!r})"
            )
    return overlay, conflicts


def merge_skill_config(project: dict, skill: dict) -> tuple[dict, list[str]]:
    """Merge a skill's config snippet `skill` into a project's config `project`.

    Project values win on every scalar and the merge never clobbers an existing
    key; lists are unioned. Returns ``(merged, conflicts)`` where `conflicts` is
    a human-readable list of scalar keys the skill wanted to change but couldn't.
    Reuses :func:`fabri.config._deep_merge` for the additive assembly once the
    overlay has been made conflict-free.
    """
    overlay, conflicts = _resolve_overlay(project, skill)
    merged = _deep_merge(project, overlay)
    return merged, conflicts


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def _copy_files(src_dir: Path, dst_dir: Path, *, force: bool) -> tuple[list[str], list[str]]:
    """Copy every regular file directly under `src_dir` into `dst_dir`, preserving
    mode (so an executable stays executable). Returns ``(copied, skipped)`` of
    destination paths; an existing destination is skipped unless `force`."""
    copied: list[str] = []
    skipped: list[str] = []
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.iterdir()):
        if not src.is_file():
            continue
        dst = dst_dir / src.name
        if dst.exists() and not force:
            skipped.append(str(dst))
            continue
        shutil.copy2(src, dst)
        copied.append(str(dst))
    return copied, skipped


def install_skill(
    skill: Skill | str,
    project_dir: str | Path = ".",
    *,
    skills_dir: str | Path | None = None,
    force: bool = False,
) -> dict:
    """Install `skill` into `project_dir`: copy its tools, copy its prompts, and
    merge its config snippet into ``<project_dir>/agent.yaml``.

    `skill` is a loaded :class:`Skill` or a name/path resolved via
    :func:`resolve_skill`. Copying skips existing files unless `force`; the config
    merge is additive (project values win, lists unioned, conflicts reported), so
    the operation is idempotent. Returns a summary dict with the keys ``skill``,
    ``tools``, ``prompts``, ``skipped``, ``config`` (the agent.yaml path or None),
    ``config_keys`` (top-level keys the merge added), and ``conflicts``.
    """
    if isinstance(skill, str):
        skill = resolve_skill(skill, skills_dir)

    root = Path(project_dir)
    root.mkdir(parents=True, exist_ok=True)

    tools: list[str] = []
    prompts: list[str] = []
    skipped: list[str] = []

    if skill.tools_dir is not None:
        copied, skip = _copy_files(skill.tools_dir, root / PROJECT_TOOLS_DIR, force=force)
        tools.extend(copied)
        skipped.extend(skip)

    if skill.prompts_dir is not None:
        copied, skip = _copy_files(skill.prompts_dir, root / PROJECT_PROMPTS_DIR, force=force)
        prompts.extend(copied)
        skipped.extend(skip)

    config_path: str | None = None
    config_keys: list[str] = []
    conflicts: list[str] = []
    if skill.config_path is not None:
        config_path, config_keys, conflicts = _merge_config_into_project(
            skill.config_path, root / "agent.yaml"
        )

    return {
        "skill": skill.name,
        "tools": tools,
        "prompts": prompts,
        "skipped": skipped,
        "config": config_path,
        "config_keys": config_keys,
        "conflicts": conflicts,
    }


def _merge_config_into_project(
    snippet_path: Path, agent_yaml: Path
) -> tuple[str, list[str], list[str]]:
    """Merge the YAML snippet at `snippet_path` into the project's `agent_yaml`,
    writing it back. When `agent_yaml` doesn't exist yet the snippet becomes the
    file. Returns ``(path, added_top_level_keys, conflicts)``."""
    try:
        snippet = yaml.safe_load(snippet_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"malformed YAML in {snippet_path}: {e}") from e
    if not isinstance(snippet, dict):
        raise SkillError(f"{snippet_path} must be a mapping")

    if agent_yaml.is_file():
        try:
            existing = yaml.safe_load(agent_yaml.read_text()) or {}
        except yaml.YAMLError as e:
            raise SkillError(f"malformed YAML in {agent_yaml}: {e}") from e
        if not isinstance(existing, dict):
            raise SkillError(f"{agent_yaml} must be a mapping at the top level")
    else:
        existing = {}

    try:
        merged, conflicts = merge_skill_config(existing, snippet)
    except ConfigError as e:
        raise SkillError(str(e)) from e
    added = [k for k in merged if k not in existing]

    agent_yaml.parent.mkdir(parents=True, exist_ok=True)
    agent_yaml.write_text(yaml.safe_dump(merged, sort_keys=False))
    return str(agent_yaml), added, conflicts


# ---------------------------------------------------------------------------
# add: scaffold a fresh skill skeleton to author
# ---------------------------------------------------------------------------


_SKILL_PROMPT_STUB = """\
# {name} skill prompt

Describe here how an agent should use the {name} capability. This template is
copied into the project's prompts/ dir on install; reference it from the agent's
system prompt.
"""

_SKILL_CONFIG_STUB = """\
# Config snippet merged into the project's agent.yaml on install. Project values
# always win; lists (e.g. tools.enabled) are unioned. Keep it minimal -- declare
# only what this skill needs.
tools:
  manifest_dir:
    - tools/agent_tools
  enabled: []
"""


def new_skill(
    name: str,
    *,
    target_dir: str | Path = DEFAULT_PROJECT_SKILLS_DIR,
    description: str | None = None,
    version: str = "0.1.0",
    force: bool = False,
) -> dict:
    """Scaffold a fresh skill skeleton at ``<target_dir>/<name>`` so a user can
    author one without hand-writing the layout: ``skill.yaml``, a ``config.yaml``
    snippet, and empty ``prompts/`` and ``tools/`` dirs (each with a
    ``.gitkeep``). Refuses to overwrite an existing skill.yaml unless `force`.
    Returns ``{"root", "created", "skipped"}``.
    """
    if not name.replace("_", "").replace("-", "").isalnum():
        raise SkillError(
            f"skill name must be alphanumeric (with - or _), got {name!r}"
        )
    root = Path(target_dir) / name
    root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": name,
        "description": description or f"{name} skill -- describe the capability it adds.",
        "version": version,
    }
    files = {
        root / SKILL_MANIFEST_NAME: yaml.safe_dump(manifest, sort_keys=False),
        root / SKILL_CONFIG_NAME: _SKILL_CONFIG_STUB,
        root / "prompts" / f"{name}.md": _SKILL_PROMPT_STUB.format(name=name),
        root / "tools" / ".gitkeep": "",
    }
    created: list[str] = []
    skipped: list[str] = []
    for path, content in files.items():
        if path.exists() and not force:
            skipped.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(str(path))
    return {"root": str(root), "created": created, "skipped": skipped}
