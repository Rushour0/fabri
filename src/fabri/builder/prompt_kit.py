"""B5 -- prompt-kit: start a new agent prompt from a proven skeleton, and parse
the user-prose / machine-memory output split consistently.

Two halves, both project-agnostic:

- :func:`render_prompt_template` / :func:`new_prompt` -- fill the proven prompt
  skeleton (``ABSOLUTE SCOPE -> RETRIEVED CONTEXT -> CHARTER -> WHAT YOU OWN ->
  DECOMPOSITION RULES -> VERIFICATION LADDER -> TOOL ROUTING -> HARD INVARIANTS
  -> OUTPUT FORMAT``) so a new agent prompt starts from a skeleton, not a blank
  file. Surfaced under the CLI's ``prompt new`` subcommand. NO LLM -- pure,
  deterministic string templating.
- :func:`split_agent_output` -- split an agent's final text on the
  ``<!-- AGENT_MEMORY -->`` marker into ``(prose, memory)`` so the human-facing
  prose and a machine-readable memory block travel together but are mined
  separately. Robust to a missing marker (returns ``(text, None)``).
  :func:`format_agent_memory` is its inverse, so a memory dict round-trips.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

# The marker that separates the user-facing prose from the machine-readable
# memory block in an agent's final output. Defined once here so the prompt
# skeleton, the splitter, and the trace miner never drift on its spelling.
AGENT_MEMORY_MARKER = "<!-- AGENT_MEMORY -->"

# A memory line is `KEY: value`; the key starts with a letter and uses only
# word chars / spaces, so a value containing a colon (or a `- ` list item)
# doesn't get misread as a key.
_MEMORY_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_ ]*):\s?(.*)$")
# A nested list line under the most recent key: `- item` or `* item`.
_MEMORY_LIST_RE = re.compile(r"^[-*]\s+(.*)$")


# ---------------------------------------------------------------------------
# render_prompt_template / new_prompt: the prompt skeleton
# ---------------------------------------------------------------------------

# The proven skeleton. Section order is load-bearing -- scope and retrieved
# context come first so the model is constrained before it reads its charter,
# and the output split is spelled out last. Placeholders are filled by
# `render_prompt_template`; RETRIEVED CONTEXT is injected by the engine at run
# time, so the template only reserves the slot.
PROMPT_SKELETON = """\
# ABSOLUTE SCOPE

{scope}

# RETRIEVED CONTEXT

Relevant prior guidelines and postmortems are injected here at run time. Treat
them as hints from past runs, not orders: prefer an approach that already worked
and avoid one that already failed.

# CHARTER

{charter}

# WHAT YOU OWN

{owns}

# DECOMPOSITION RULES

{decomposition_rules}

# VERIFICATION LADDER

Climb the cheapest rung that proves the work, and stop there:

{verification_ladder}

# TOOL ROUTING

{tool_routing}

# HARD INVARIANTS

{invariants}

# OUTPUT FORMAT

Reply with the human-facing answer first, in plain prose. If this run produced
durable facts worth remembering, append a machine-readable block fenced by the
marker below -- everything under it is parsed into memory, not shown to the user:

{marker}
TASK: <one line restating what you did>
OUTCOME: <success | partial | failed>
CHANGES:
- <a concrete thing you changed or produced>
- <another, if any>

Omit the marker entirely when there is nothing durable to record.
"""

# Neutral, generic fallbacks so a freshly rendered prompt is coherent and
# fillable rather than empty. None of these name a domain, app, or game.
_DEFAULT_SCOPE = (
    "Work ONLY on the task described below. Do not expand scope, refactor "
    "unrelated areas, or take on adjacent work you were not explicitly asked for."
)
_DEFAULT_OWNS = [
    "<the concrete artifact or surface you are responsible for>",
    "<a second area you own end-to-end, if any>",
]
_DEFAULT_DECOMPOSITION = [
    "Split the task only when a part is independent and large enough to be worth "
    "isolating; keep dependent, sequential steps inline.",
    "Do the work inline by default -- a sub-agent re-runs the whole loop, so an "
    "unnecessary one multiplies cost.",
]
_DEFAULT_VERIFICATION = [
    "Confirm your output is well-formed (it parses / matches the required shape).",
    "Confirm it satisfies the task's explicit requirements.",
    "Escalate to an expensive check (full run, external call) only when the cheap "
    "checks already pass.",
]
_DEFAULT_TOOLS = [
    "<tool_name> -- <when to reach for it and what it returns>",
]
_DEFAULT_INVARIANTS = [
    "Never invent data or tool results -- act only on what you actually observed.",
    "Stay within the step and cost budget; prefer one decisive action over many "
    "exploratory probes.",
    "Stop when the task is done -- do not gold-plate.",
]


def _bullets(items: Sequence[str]) -> str:
    """Render a list of section lines as `- ` bullets, one per line."""
    return "\n".join(f"- {item}" for item in items)


def _tool_routing(tools: Sequence[str]) -> str:
    """A bare tool name becomes a `name -- <when to use>` fill-in line; a line
    that already carries its own routing note (contains ` -- `) is kept as-is."""
    lines = []
    for tool in tools:
        if "--" in tool:
            lines.append(f"- {tool}")
        else:
            lines.append(f"- {tool} -- <when to reach for it and what it returns>")
    return "\n".join(lines)


def render_prompt_template(
    role: str,
    *,
    owns: Sequence[str] | None = None,
    tools: Sequence[str] | None = None,
    scope: str | None = None,
    charter: str | None = None,
    decomposition_rules: Sequence[str] | None = None,
    verification_ladder: Sequence[str] | None = None,
    invariants: Sequence[str] | None = None,
) -> str:
    """Fill :data:`PROMPT_SKELETON` for an agent whose role is `role`.

    Every section has a neutral, generic default so the result is a coherent
    starting point a human edits, not a blank file. `owns` and `tools` are the
    two most worth passing: they seed WHAT YOU OWN and TOOL ROUTING with the
    caller's own list. No LLM, fully deterministic.
    """
    charter_text = charter or (
        f"You are {role}. State, in one short paragraph, the mission you exist "
        f"to accomplish and the standard you hold yourself to."
    )
    return PROMPT_SKELETON.format(
        scope=scope or _DEFAULT_SCOPE,
        charter=charter_text,
        owns=_bullets(owns or _DEFAULT_OWNS),
        decomposition_rules=_bullets(decomposition_rules or _DEFAULT_DECOMPOSITION),
        verification_ladder=_bullets(verification_ladder or _DEFAULT_VERIFICATION),
        tool_routing=_tool_routing(tools or _DEFAULT_TOOLS),
        invariants=_bullets(invariants or _DEFAULT_INVARIANTS),
        marker=AGENT_MEMORY_MARKER,
    )


def new_prompt(
    name: str,
    *,
    role: str | None = None,
    output: str | Path | None = None,
    force: bool = False,
    **kwargs,
) -> dict:
    """Render a starter prompt for `name` and write it to a `.md` file.

    `role` defaults to `name`; `output` defaults to ``<name>.prompt.md`` in the
    cwd. Extra keyword args pass straight through to
    :func:`render_prompt_template` (owns, tools, ...). Returns
    ``{"path": str, "created": bool}``; refuses to clobber an existing file
    unless `force`.
    """
    path = Path(output) if output is not None else Path(f"{name}.prompt.md")
    if path.exists() and not force:
        return {"path": str(path), "created": False}
    text = render_prompt_template(role or name, **kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return {"path": str(path), "created": True}


# ---------------------------------------------------------------------------
# split_agent_output / format_agent_memory: the prose / memory output split
# ---------------------------------------------------------------------------


def split_agent_output(text: str) -> tuple[str, dict | None]:
    """Split an agent's final `text` on :data:`AGENT_MEMORY_MARKER`.

    Returns ``(prose, memory)`` where `prose` is everything before the marker
    (the human-facing answer) and `memory` is the parsed structured block below
    it, or ``None`` when the marker is absent. The block is a simple
    ``KEY: value`` format; a key whose value is empty followed by ``- item``
    lines becomes a list (e.g. ``CHANGES:`` -> ``["...", "..."]``). Robust to a
    missing marker, an empty block, and stray lines.
    """
    if text is None:
        return "", None
    if AGENT_MEMORY_MARKER not in text:
        return text, None

    prose, _, block = text.partition(AGENT_MEMORY_MARKER)
    memory = _parse_memory_block(block)
    return prose.rstrip(), memory


def _parse_memory_block(block: str) -> dict | None:
    """Parse the lines under the marker into a dict. Returns None when the block
    holds nothing parseable, so a bare marker with no content reads as 'no
    memory' rather than an empty dict."""
    memory: dict[str, object] = {}
    current_key: str | None = None

    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue

        list_match = _MEMORY_LIST_RE.match(line)
        if list_match and current_key is not None:
            # Promote the current key's value to a list and append the item.
            existing = memory.get(current_key)
            if not isinstance(existing, list):
                memory[current_key] = [] if existing in (None, "") else [existing]
            memory[current_key].append(list_match.group(1).strip())  # type: ignore[union-attr]
            continue

        key_match = _MEMORY_KEY_RE.match(line)
        if key_match:
            current_key = key_match.group(1).strip()
            memory[current_key] = key_match.group(2).strip()
            continue
        # A non-matching, non-empty line is treated as a continuation of the
        # current string value rather than dropped, so multi-line values survive.
        if current_key is not None and isinstance(memory.get(current_key), str):
            sep = " " if memory[current_key] else ""
            memory[current_key] = f"{memory[current_key]}{sep}{line}"

    return memory or None


def format_agent_memory(memory: dict) -> str:
    """Render a memory dict back into a marker-fenced block -- the inverse of
    :func:`split_agent_output` for list/string values, so a dict round-trips.
    String values become ``KEY: value``; list values become ``KEY:`` followed by
    ``- item`` lines."""
    lines = [AGENT_MEMORY_MARKER]
    for key, value in memory.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)
