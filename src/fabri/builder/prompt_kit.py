"""B5 -- prompt-kit: start a new agent prompt from a proven skeleton, and parse
the user-prose / machine-memory output split consistently.

Two halves, both project-agnostic:

- :func:`render_prompt_template` / :func:`new_prompt` -- fill the proven prompt
  skeleton (``SCOPE -> RETRIEVED CONTEXT -> CHARTER -> WHAT YOU OWN ->
  DECOMPOSITION RULES -> VERIFICATION LADDER -> TOOL ROUTING -> WORKING RULES
  -> OUTPUT FORMAT``) so a new agent prompt starts from a skeleton, not a blank
  file. Surfaced under the CLI's ``prompt new`` subcommand. NO LLM -- pure,
  deterministic string templating.
- :func:`split_agent_output` -- split an agent's final text on the
  ``<!-- AGENT_MEMORY -->`` marker into ``(prose, memory)`` so the human-facing
  prose and a machine-readable memory block travel together but are mined
  separately. Robust to a missing marker (returns ``(text, None)``).
  :func:`format_agent_memory` is its inverse, so a memory dict round-trips.
- :func:`render_task_brief` -- the brief a parent hands a delegated child. It
  has the four parts a delegated task needs (objective, output format, tool
  guidance, task boundaries). Code supplies the fixed parts; only the task
  details come from a model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from fabri.memory.output import (
    AGENT_MEMORY_MARKER,
    format_agent_memory,
    split_agent_output,
)


# ---------------------------------------------------------------------------
# render_prompt_template / new_prompt: the prompt skeleton
# ---------------------------------------------------------------------------

# The proven skeleton. Section order is load-bearing -- scope and retrieved
# context come first so the model is constrained before it reads its charter,
# and the output split is spelled out last. Placeholders are filled by
# `render_prompt_template`; RETRIEVED CONTEXT is injected by the engine at run
# time, so the template only reserves the slot.
PROMPT_SKELETON = """\
# SCOPE

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

# WORKING RULES

These are defaults that hold up in most runs. Each says why, so you can judge
the rare case where it does not fit.

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
    "Work on the task you were given. Leave unrelated areas and adjacent work "
    "alone, even when they look easy to improve: changes nobody asked for are "
    "hard to review and can break things the requester depends on."
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
    "Act only on what you observed in tool results. A guessed value looks the "
    "same as a real one to whoever reads your answer, so say when you are unsure.",
    "Prefer one decisive action over many exploratory probes, because every step "
    "spends budget the rest of the task may need.",
    "Stop when the task is done. Extra polish costs time and adds changes to review.",
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
# render_task_brief: the four parts of a delegated task
# ---------------------------------------------------------------------------

# A child starts with none of the parent's context, so a vague brief makes it
# duplicate work, wander, or return the wrong shape. Anthropic's multi-agent
# research write-up names four parts every delegated task needs; this renders
# them in a fixed order. The fixed parts (output contract, tool notes, default
# boundaries) come from code so they read the same on every spawn.
TASK_BRIEF_SKELETON = """\
## Objective

{objective}

## Output format

{output_format}

## Tools

{tool_guidance}

## Boundaries

{boundaries}
"""

DEFAULT_CHILD_OUTPUT_FORMAT = (
    "Reply with a short summary the parent can act on: what you found or changed, "
    "the evidence (file paths, ids, numbers), and anything left undone. Aim for "
    "well under 1,500 words; the parent only sees a capped summary, and your full "
    "transcript stays in your trace."
)
DEFAULT_CHILD_BOUNDARIES = [
    "Do only this task. The parent owns the overall plan, so leave other parts to it.",
    "If the task turns out to need a tool you do not have, say so and stop rather "
    "than working around it; the parent can grant it or do that part itself.",
]


def render_task_brief(
    objective: str,
    *,
    tools: Sequence[str] | None = None,
    tool_notes: dict[str, str] | None = None,
    output_format: str | None = None,
    boundaries: Sequence[str] | None = None,
) -> str:
    """The brief for one delegated task, with objective, output format, tool
    guidance and task boundaries in that order.

    ``objective`` is the only part that normally comes from a model. ``tools``
    is the child's tool list (from code, after scoping); ``tool_notes`` maps a
    tool name to a one-line note on when to use it. Extra ``boundaries`` are
    added after the defaults. Deterministic: the same inputs give the same
    text."""
    objective = (objective or "").strip()
    if not objective:
        raise ValueError("render_task_brief: objective is required")
    notes = tool_notes or {}
    if tools:
        tool_lines = "\n".join(
            f"- {t} -- {notes[t]}" if t in notes else f"- {t}" for t in tools
        )
        tool_guidance = (
            "You have only these tools. Start with the cheapest one that can "
            "answer the question:\n" + tool_lines
        )
    else:
        tool_guidance = "You have no tools for this task. Answer from the brief alone."
    return TASK_BRIEF_SKELETON.format(
        objective=objective,
        output_format=output_format or DEFAULT_CHILD_OUTPUT_FORMAT,
        tool_guidance=tool_guidance,
        boundaries=_bullets([*DEFAULT_CHILD_BOUNDARIES, *(boundaries or [])]),
    )
