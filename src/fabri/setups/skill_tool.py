"""The `load_skill` meta tool: progressive disclosure for on-demand skills.

The system prompt lists an on-demand skill by name and description only.
The agent calls `load_skill(name)` to read the full text, and
`load_skill(name, reference="references/x.md")` to read one of the skill's
reference files. Loading a skill also exposes the tools the skill names, so
the model can call them in the same run even when tool retrieval hid them.
"""
from __future__ import annotations

from fabri.setups.layers import SetupError
from fabri.setups.resolver import ResolvedSetup
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ToolRegistry
from fabri.tools.result import tool_error, tool_ok

LOAD_SKILL = "load_skill"


def register_load_skill(registry: ToolRegistry, resolved: ResolvedSetup, agent_type: str) -> bool:
    """Add `load_skill` for the type's on-demand skills. Returns False (and
    adds nothing) when the type has none."""
    names = resolved.on_demand_skills(agent_type)
    if not names:
        return False

    def load_skill(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return tool_error(f"load_skill: `name` is required. Skills: {', '.join(names)}")
        if name not in names:
            return tool_error(f"load_skill: no on-demand skill named {name!r}. Skills: {', '.join(names)}")
        reference = args.get("reference")
        try:
            result = resolved.load_skill(agent_type, name, str(reference) if reference else None)
        except (SetupError, OSError) as exc:
            return tool_error(f"load_skill: {exc}")
        if not reference:
            registry.request_exposure(result.get("tools") or [])
        return tool_ok(result)

    registry.register_callable(ToolManifest(
        name=LOAD_SKILL, title="Load a skill",
        description="Read the full instructions of a skill listed under 'Skills to load on demand'. "
                    "Pass `reference` to read one of the skill's reference files instead. "
                    "The tools the skill uses can be called right after.",
        command=[], input_schema={"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name from the on-demand list.", "enum": names},
            "reference": {"type": "string", "description": "Optional reference file path, e.g. references/api.md."}},
            "required": ["name"]},
        output_schema={"type": "object"}, approval="read"), load_skill)
    return True
