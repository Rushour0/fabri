"""fabri's *builder* layer (Track B): turn intent into a running agent.

The engine (``core/``, ``orchestrator/``, ``memory/``, ``tools/``) runs and
learns; the builder makes a *new* product on it fast. This package holds the
builder primitives -- the B2 tool-writer, which scaffolds a schema-tightened
tool from a Python signature or a description, validates a manifest, and runs a
tool locally through the existing runner; the B5 prompt-kit, which starts a
new agent prompt from a proven skeleton and parses the user-prose /
machine-memory output split; the B6 wave planner, which layers declared
dependency edges into serial/parallel waves with auto-assigned
``parallel_group`` tags; and the B4 skills registry, which installs a reusable
bundle of (prompt + tool manifests + config snippet) into a project so a
capability carries across products.
"""
from fabri.builder.discovery import (
    build_dry_run_plan,
    filter_tools,
    render_dry_run_plan,
    render_tools_listing,
)
from fabri.builder.ideator import (
    IDEATION_SPEC_SCHEMA,
    IdeatorError,
    ideate,
    propose_spec,
    scaffold_from_spec,
    spec_to_config,
)
from fabri.builder.prompt_kit import (
    AGENT_MEMORY_MARKER,
    format_agent_memory,
    new_prompt,
    render_prompt_template,
    split_agent_output,
)
from fabri.builder.skills import (
    Skill,
    SkillError,
    discover_skills,
    install_skill,
    load_skill,
    merge_skill_config,
    new_skill,
    render_skills_listing,
    resolve_skill,
)
from fabri.builder.tool_writer import (
    new_tool,
    parse_signature,
    test_tool,
    validate_manifest,
)
from fabri.builder.waves import (
    SpawnDescriptor,
    Wave,
    WaveError,
    WavePlan,
    plan_waves,
    spawn_descriptors,
)

__all__ = [
    "AGENT_MEMORY_MARKER",
    "IDEATION_SPEC_SCHEMA",
    "IdeatorError",
    "Skill",
    "SkillError",
    "SpawnDescriptor",
    "Wave",
    "WaveError",
    "WavePlan",
    "build_dry_run_plan",
    "discover_skills",
    "filter_tools",
    "format_agent_memory",
    "ideate",
    "install_skill",
    "load_skill",
    "merge_skill_config",
    "new_prompt",
    "new_skill",
    "new_tool",
    "parse_signature",
    "plan_waves",
    "propose_spec",
    "render_skills_listing",
    "resolve_skill",
    "scaffold_from_spec",
    "spec_to_config",
    "render_dry_run_plan",
    "render_prompt_template",
    "render_tools_listing",
    "spawn_descriptors",
    "split_agent_output",
    "test_tool",
    "validate_manifest",
]
