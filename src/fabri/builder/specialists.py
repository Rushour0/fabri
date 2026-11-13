"""Specialist-profile factory: turn a delegated task into a narrow child agent.

Given a task and the parent's tools, :func:`make_specialist` returns a
:class:`SpecialistProfile` with a name, a system prompt rendered by the prompt
kit, and a tool list that is a subset of the parent's. :func:`spawn_args` then
builds the ``spawn_subagent`` arguments, with the tool scope set by code.

Who decides what:

- A model (the "designer") fills only the task-specific parts: the role, what
  the specialist owns, which of the offered tools it needs, and a one-line
  note per tool. It sees the parent's tool names and nothing else.
- Code owns the rest. Tool names the designer invents are dropped, the list is
  sorted, the prompt skeleton and its working rules come from
  :mod:`fabri.builder.prompt_kit`, and the per-task brief is rendered by
  :func:`render_task_brief` at spawn time.

Profiles are cached by a hash of their inputs (task kind, the parent's tool
names, the factory version), in memory and optionally on disk. A hit skips the
designer call, so the same kind of task gets the same prompt and tool list
every time. That keeps the child's system prompt byte-stable, which is what
lets the provider's prompt cache work across spawns.

Specialists are opt-in. Nothing in the agent loop calls this module; a host
or a custom tool does.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from fabri.builder.prompt_kit import render_prompt_template, render_task_brief
from fabri.core import structured
from fabri.core.logging_setup import get_logger

logger = get_logger()

# Bump when the designer prompt, the schema, or the rendering changes, so old
# cached profiles stop matching.
FACTORY_VERSION = 1

DESIGNER_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "owns": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
        "tool_notes": {"type": "object"},
    },
    "required": ["role", "tools"],
}

DESIGNER_SYSTEM = """\
You design the profile of one specialist agent that will do a delegated task.
The specialist starts with no context except what its profile and brief give
it, so be concrete.

You will get the task and the list of tools the parent can lend. Pick the
smallest set of those tools that can do the task: every extra tool is one more
thing the specialist can misuse, and tools that write or send need a stronger
reason than tools that read. Use only names from the list.

Reply with one JSON object and nothing else:
{"role": "<a short job title, for example 'test failure triager'>",
 "owns": ["<what the specialist is responsible for producing>", "..."],
 "tools": ["<tool name from the list>", "..."],
 "tool_notes": {"<tool name>": "<one line: when to use it for this task>"}}
"""


class SpecialistError(RuntimeError):
    """The designer call failed or returned something unusable."""


@dataclass(frozen=True)
class SpecialistProfile:
    name: str
    role: str
    system_prompt: str
    tools: tuple[str, ...]
    tool_notes: dict[str, str] = field(default_factory=dict)
    cache_key: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tools"] = list(self.tools)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SpecialistProfile":
        return cls(
            name=str(data["name"]), role=str(data["role"]), system_prompt=str(data["system_prompt"]),
            tools=tuple(data.get("tools") or ()), tool_notes=dict(data.get("tool_notes") or {}),
            cache_key=str(data.get("cache_key") or ""),
        )


def profile_cache_key(task_kind: str, parent_tools: Sequence[str]) -> str:
    """A stable hash of everything that shapes a profile. ``task_kind`` is
    normalized (case and whitespace) so trivial rewordings still hit."""
    kind = " ".join((task_kind or "").lower().split())
    payload = json.dumps(
        {"v": FACTORY_VERSION, "kind": kind, "tools": sorted(set(parent_tools))},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ProfileCache:
    """Profiles by cache key, in memory, mirrored to ``directory`` as one
    JSON file per key when a directory is given. Unreadable files are misses."""

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory is not None else None
        self._mem: dict[str, SpecialistProfile] = {}

    def get(self, key: str) -> SpecialistProfile | None:
        if key in self._mem:
            return self._mem[key]
        if self.directory is None:
            return None
        path = self.directory / f"{key}.json"
        try:
            profile = SpecialistProfile.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self._mem[key] = profile
        return profile

    def put(self, profile: SpecialistProfile) -> None:
        self._mem[profile.cache_key] = profile
        if self.directory is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{profile.cache_key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)


def default_cache_dir() -> Path:
    from fabri.paths import home

    return home() / ".fabri" / "specialists"


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9a-z]+", "_", (text or "").lower()).strip("_")
    return s[:40] or "specialist"


def _design(task: str, parent_tools: Sequence[str], llm) -> dict:
    prompt = (
        f"Task:\n{task.strip()}\n\n"
        "Tools the parent can lend (use only these names):\n"
        + "\n".join(f"- {t}" for t in sorted(set(parent_tools)))
    )
    try:
        resp = llm.step(DESIGNER_SYSTEM, [{"role": "user", "content": prompt}])
    except Exception as e:  # noqa: BLE001 -- any provider failure
        raise SpecialistError(f"specialist designer call failed: {e}") from e
    value, errors = structured.parse_response(resp.final_text or "", DESIGNER_SCHEMA)
    if not isinstance(value, dict) or errors:
        raise SpecialistError("specialist designer returned no usable profile: "
                              + ("; ".join(errors) if errors else "not a JSON object"))
    return value


def make_specialist(
    task: str,
    parent_tools: Sequence[str],
    llm,
    *,
    task_kind: str | None = None,
    cache: ProfileCache | None = None,
) -> SpecialistProfile:
    """The profile for a specialist that does ``task`` with a subset of
    ``parent_tools``. ``task_kind`` is the cache handle (defaults to the task
    text); pass a short stable label such as ``"triage failing test"`` when
    many tasks of one kind should share a profile. ``llm`` runs only on a
    cache miss. Raises :class:`SpecialistError` when the designer fails or
    picks no tool the parent has."""
    if not task or not task.strip():
        raise SpecialistError("task must be non-empty")
    key = profile_cache_key(task_kind or task, parent_tools)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            logger.info("specialist profile cache hit %s (%s)", key, hit.name)
            return hit
    if llm is None:
        raise SpecialistError("no LLM to design a specialist and no cached profile")

    spec = _design(task, parent_tools, llm)
    offered = set(parent_tools)
    chosen = sorted({t for t in spec.get("tools") or [] if isinstance(t, str) and t in offered})
    invented = sorted({t for t in spec.get("tools") or [] if isinstance(t, str)} - offered)
    if invented:
        logger.info("specialist designer named tools the parent lacks; dropped: %s", invented)
    if not chosen:
        raise SpecialistError("specialist designer picked no tool the parent has")
    raw_notes = spec.get("tool_notes") if isinstance(spec.get("tool_notes"), dict) else {}
    notes = {t: " ".join(str(raw_notes[t]).split())[:200] for t in chosen if t in raw_notes}
    role = " ".join(str(spec["role"]).split())[:80] or "specialist"
    owns = [" ".join(str(o).split())[:200] for o in (spec.get("owns") or []) if str(o).strip()][:5]

    system_prompt = render_prompt_template(
        role,
        owns=owns or None,
        tools=[f"{t} -- {notes[t]}" if t in notes else t for t in chosen],
        charter=(
            f"You are a {role} working on one task delegated by a parent agent. "
            "Your brief arrives as the user message. Do that task well and hand "
            "back a summary the parent can act on without rereading your work."
        ),
    )
    profile = SpecialistProfile(
        name=f"{_slug(role)}_{key[:6]}", role=role, system_prompt=system_prompt,
        tools=tuple(chosen), tool_notes=notes, cache_key=key,
    )
    if cache is not None:
        cache.put(profile)
    return profile


def spawn_args(
    profile: SpecialistProfile,
    task: str,
    config_path: str,
    *,
    boundaries: Sequence[str] | None = None,
    **extra,
) -> dict:
    """``spawn_subagent`` arguments for one task run by ``profile``. The tool
    scope is the profile's tool list (code, not the model), the stable system
    prompt goes in ``system_prompt_inline``, and the task-specific brief goes
    in ``task`` so the system prompt stays cacheable."""
    args = {
        "config_path": config_path,
        "task": render_task_brief(task, tools=profile.tools, tool_notes=profile.tool_notes,
                                  boundaries=boundaries),
        "system_prompt_inline": profile.system_prompt,
        "tool_scope": list(profile.tools),
    }
    args.update(extra)
    return args
