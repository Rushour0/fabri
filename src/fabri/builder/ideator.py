"""B1 -- ideator: turn a one-sentence product idea into a *reviewable* agent
scaffold a human can edit, then ``fabri run``.

The flow is two deterministic-once-you-have-the-spec halves:

- :func:`propose_spec` -- the ONLY LLM step. Asks the configured backend for a
  structured spec (validated against :data:`IDEATION_SPEC_SCHEMA` via the
  dependency-free ``core/structured.py`` validator). The backend is injected so
  the whole path is unit-testable with a scripted backend; it raises a clear
  :class:`IdeatorError` when no backend is available (no API key) instead of
  crashing on the network.
- :func:`scaffold_from_spec` -- pure file writing, no LLM. From the spec it
  writes an ``agent.yaml`` that ``config.load_config`` accepts, prompt ``.md``
  files via the B5 prompt-kit, and a schema-tightened tool stub per
  ``tools_to_build`` entry via the B2 tool-writer.

It **emits files for review only** -- it never auto-applies a spec to a running
agent, and it refuses to write into an existing non-empty directory unless
``force`` is set, so an existing project is never modified in place.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from fabri.builder.prompt_kit import new_prompt
from fabri.builder.tool_writer import check_schema, new_tool
from fabri.core import structured
from fabri.core.llm import Provider
from fabri.core.logging_setup import get_logger

logger = get_logger()


class IdeatorError(ValueError):
    """The ideator could not produce a usable scaffold: no LLM backend (no API
    key), the model's reply wasn't a spec that matches
    :data:`IDEATION_SPEC_SCHEMA`, or the target directory already holds a
    project. The CLI catches this and prints a clean message + exit 1 rather
    than letting a raw traceback out."""


# The shape the model must return, expressed in the JSON-Schema subset
# `core/structured.py` validates. Only the fields the scaffolder actually
# consumes are required; everything else degrades to a sane default so a
# slightly-thin spec still produces a runnable scaffold.
IDEATION_SPEC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "agent_name": {"type": "string"},
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "model": {"type": "string"},
                    "max_tokens": {"type": "integer"},
                },
                "required": ["role", "model"],
            },
        },
        "budgets": {
            "type": "object",
            "properties": {
                "max_steps": {"type": "integer"},
                "max_cost_usd": {"type": "number"},
            },
        },
        "domains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt_summary": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "tools_to_build": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
            },
        },
        "system_prompt_prefix": {"type": "string"},
        "response_schema": {"type": "object"},
    },
    "required": [
        "agent_name",
        "roles",
        "budgets",
        "domains",
        "tools_to_build",
        "system_prompt_prefix",
    ],
}

# Roles that map onto a dedicated `llm.<role>` override block in agent.yaml; any
# other role name (notably "main") is folded into the top-level llm.* defaults.
_OVERRIDE_ROLES = ("decompose", "planner", "narrator")

# Builtins every scaffold turns on so a freshly-generated agent can read/write
# its workspace out of the box; the generated tools are appended to this.
_BASE_TOOLS = ("read_file", "write_file", "list_dir")

_IDEATE_SYSTEM = (
    "You are an agent architect. Given a one-sentence product idea, design a "
    "fabri agent and reply with ONE JSON object (no prose, no code fences) "
    "matching this shape:\n"
    '{"agent_name": str, '
    '"roles": [{"role": "main|decompose|planner|narrator", "model": str, "max_tokens": int}], '
    '"budgets": {"max_steps": int, "max_cost_usd": number}, '
    '"domains": [{"name": str, "prompt_summary": str}], '
    '"tools_to_build": [{"name": str, "description": str}], '
    '"system_prompt_prefix": str, '
    '"response_schema": {JSON-Schema object, optional}}\n'
    "Keep it minimal and project-agnostic: a handful of focused tools, one "
    "domain per area of responsibility, neutral names."
)


# ---------------------------------------------------------------------------
# propose_spec: the single LLM step
# ---------------------------------------------------------------------------


def propose_spec(idea: str, llm) -> dict:
    """Ask `llm` for a structured agent spec for the product `idea`.

    `llm` is any object exposing ``step(system, messages) -> LLMResponse`` (the
    real backend or a scripted one), injected so this is testable offline.
    Returns the validated spec dict. Raises :class:`IdeatorError` when no
    backend is supplied (no API key), the call fails, or the reply doesn't
    match :data:`IDEATION_SPEC_SCHEMA`.
    """
    if not idea or not idea.strip():
        raise IdeatorError("idea must be a non-empty sentence")
    if llm is None:
        raise IdeatorError(
            "no LLM backend available: `fabri ideate` needs a model to draft the "
            "spec -- set your provider API key (e.g. export ANTHROPIC_API_KEY=...) "
            "and try again."
        )
    prompt = (
        f"Product idea: {idea.strip()}\n"
        "Design the fabri agent that delivers it."
    )
    try:
        resp = llm.step(_IDEATE_SYSTEM, [{"role": "user", "content": prompt}])
    except Exception as e:  # network/SDK/anything -- surface it cleanly
        raise IdeatorError(f"the ideation model call failed: {e}") from e

    value, errors = structured.parse_response(resp.final_text or "", IDEATION_SPEC_SCHEMA)
    if not isinstance(value, dict) or errors:
        detail = "; ".join(errors) if errors else "reply was not a JSON object"
        raise IdeatorError(f"the model did not return a usable spec: {detail}")
    return value


# ---------------------------------------------------------------------------
# spec -> agent.yaml config
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Kebab-case slug for directory / collection names. Falls back to "agent"
    when nothing alphanumeric survives."""
    s = re.sub(r"[^0-9a-zA-Z]+", "-", (name or "").strip().lower()).strip("-")
    return s or "agent"


def _identifier(name: str) -> str:
    """snake_case identifier for a tool/prompt name -- alphanumeric+underscore,
    never leading with a digit (so it's a valid file stem and tool name).
    Returns "" when nothing usable survives so the caller can skip it."""
    ident = re.sub(r"[^0-9a-zA-Z]+", "_", (name or "").strip().lower()).strip("_")
    if ident and ident[0].isdigit():
        ident = f"t_{ident}"
    return ident


def _spec_tools(spec: dict) -> list[tuple[str, str]]:
    """Resolve `tools_to_build` into a deduped list of ``(identifier, desc)``,
    dropping entries whose name yields no usable identifier."""
    seen: set[str] = set()
    tools: list[tuple[str, str]] = []
    for entry in spec.get("tools_to_build") or []:
        if not isinstance(entry, dict):
            continue
        ident = _identifier(entry.get("name", ""))
        if not ident or ident in seen:
            continue
        seen.add(ident)
        desc = entry.get("description") or f"Tool {ident} -- describe what this does."
        tools.append((ident, desc))
    return tools


def spec_to_config(spec: dict) -> dict:
    """Build the agent.yaml config dict from `spec`. The result is shaped so
    ``config.load_config`` (which deep-merges over DEFAULT_CONFIG) accepts it
    unchanged. Memory defaults to the sqlite backend so the review scaffold
    runs without docker."""
    name = spec.get("agent_name") or "agent"
    slug = _slug(name)
    roles = {
        r.get("role"): r
        for r in (spec.get("roles") or [])
        if isinstance(r, dict) and r.get("role")
    }
    main = roles.get("main", {})
    budgets = spec.get("budgets") or {}

    agent_cfg: dict = {
        "name": name,
        "max_steps": budgets.get("max_steps") or 10,
    }
    if budgets.get("max_cost_usd") is not None:
        agent_cfg["max_cost_usd"] = budgets["max_cost_usd"]
    prefix = spec.get("system_prompt_prefix")
    if prefix:
        agent_cfg["system_prompt_prefix"] = prefix
    response_schema = spec.get("response_schema")
    if isinstance(response_schema, dict) and response_schema:
        schema_errors = check_schema(response_schema)
        if schema_errors:
            logger.warning(
                "ideate: dropping response_schema -- not a valid JSON-Schema-subset (%s)",
                "; ".join(schema_errors),
            )
        else:
            agent_cfg["response_schema"] = response_schema

    llm_cfg: dict = {
        "provider": Provider.ANTHROPIC.value,
        "model": main.get("model") or "claude-sonnet-4-6",
        "max_tokens": main.get("max_tokens") or 1024,
        "api_key_env": "ANTHROPIC_API_KEY",
    }
    for role_name in _OVERRIDE_ROLES:
        role = roles.get(role_name)
        if not (isinstance(role, dict) and role.get("model")):
            continue
        entry: dict = {"model": role["model"]}
        if role.get("max_tokens"):
            entry["max_tokens"] = role["max_tokens"]
        llm_cfg[role_name] = entry

    tool_idents = [ident for ident, _ in _spec_tools(spec)]
    tools_cfg = {
        "manifest_dir": ["builtin", "tools/agent_tools"],
        "enabled": [*_BASE_TOOLS, *tool_idents],
        "sandbox_root": ".",
        "result_format": "toon",
    }
    memory_cfg = {
        "backend": "sqlite",
        "collection": slug.replace("-", "_"),
        "sqlite_path": f".fabri/{slug}.db",
    }
    return {"agent": agent_cfg, "llm": llm_cfg, "tools": tools_cfg, "memory": memory_cfg}


# ---------------------------------------------------------------------------
# scaffold_from_spec: write the reviewable directory
# ---------------------------------------------------------------------------


def scaffold_from_spec(
    spec: dict,
    out_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> dict:
    """Write a reviewable scaffold for `spec` into `out_dir` (default
    ``./<agent_name>-agent``) and return a summary.

    Writes ``agent.yaml`` (valid for ``load_config``), ``prompts/system.md``
    plus one ``prompts/<domain>.md`` per declared domain via the B5 prompt-kit,
    and a schema-tightened tool stub per ``tools_to_build`` entry via the B2
    tool-writer. Refuses to write into an existing non-empty directory unless
    `force`, so an existing project is never modified in place.

    Returns ``{"root", "agent_yaml", "prompts", "tools", "skipped",
    "next_command"}``.
    """
    name = spec.get("agent_name") or "agent"
    slug = _slug(name)
    root = Path(out_dir) if out_dir else Path(f"{slug}-agent")

    if root.exists() and any(root.iterdir()) and not force:
        raise IdeatorError(
            f"{root} already exists and is not empty; choose another --out dir or "
            f"pass --force. `fabri ideate` never modifies an existing project in place."
        )
    root.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[str] = []
    tools = _spec_tools(spec)
    tool_idents = [ident for ident, _ in tools]

    # 1) agent.yaml
    config = spec_to_config(spec)
    yaml_path = root / "agent.yaml"
    if yaml_path.exists() and not force:
        skipped.append(str(yaml_path))
    else:
        yaml_path.write_text(yaml.safe_dump(config, sort_keys=False))
        written.append(str(yaml_path))

    # 2) prompts -- a main system prompt + one per domain, via the prompt-kit.
    prompts_dir = root / "prompts"
    prompt_paths: list[str] = []
    main_res = new_prompt(
        slug,
        role=name,
        output=prompts_dir / "system.md",
        charter=spec.get("system_prompt_prefix") or None,
        tools=tool_idents or None,
        force=force,
    )
    (prompt_paths if main_res["created"] else skipped).append(main_res["path"])

    for domain in spec.get("domains") or []:
        if not isinstance(domain, dict) or not domain.get("name"):
            continue
        dslug = _slug(domain["name"])
        res = new_prompt(
            dslug,
            role=domain["name"],
            output=prompts_dir / f"{dslug}.md",
            charter=domain.get("prompt_summary") or None,
            tools=tool_idents or None,
            force=force,
        )
        (prompt_paths if res["created"] else skipped).append(res["path"])

    # 3) tool stubs -- one schema-tightened scaffold per tools_to_build entry.
    # No LLM here (llm=None): scaffolding is deterministic and offline; the
    # human tightens each schema during review.
    tools_dir = root / "tools" / "agent_tools"
    tool_paths: list[str] = []
    for ident, desc in tools:
        result = new_tool(
            ident,
            from_desc=desc,
            target_dir=tools_dir,
            llm=None,
            force=force,
        )
        tool_paths.extend(str(tools_dir / f) for f in result["created"])
        skipped.extend(str(tools_dir / f) for f in result["skipped"])

    next_command = f'fabri --config {yaml_path} run "<your task>"'
    return {
        "root": str(root),
        "agent_yaml": str(yaml_path),
        "prompts": prompt_paths,
        "tools": tool_paths,
        "skipped": skipped,
        "next_command": next_command,
    }


def ideate(
    idea: str,
    llm,
    *,
    out_dir: str | Path | None = None,
    force: bool = False,
) -> dict:
    """End-to-end B1: propose a spec for `idea` via `llm`, then write the
    reviewable scaffold. `llm` is injected so this is testable with a scripted
    backend. The returned summary carries the proposed `spec` alongside the
    :func:`scaffold_from_spec` result so a caller can show what was decided."""
    spec = propose_spec(idea, llm)
    summary = scaffold_from_spec(spec, out_dir, force=force)
    summary["spec"] = spec
    return summary
