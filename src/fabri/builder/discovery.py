"""B3 -- runner & discovery ergonomics: answer "what tools do I have?" and
"what would this run actually do?" without grepping ``tools/examples`` or
exporting an API key.

Pure, side-effect-free helpers the CLI renders:

- :func:`filter_tools` -- project a resolved :class:`ToolRegistry` onto
  ``(name, description)`` pairs, optionally filtered by a case-insensitive
  substring over name+description (``fabri tools [--search]``).
- :func:`build_dry_run_plan` -- assemble the resolved-config summary + the tool
  definitions that *would* be sent to the model, without building any LLM
  backend or opening the memory store (``fabri run --dry-run``).
- :func:`render_tools_listing` / :func:`render_dry_run_plan` -- text rendering
  of the two, kept here (not in cli.py) so they're unit-testable.

These never touch the network: they read a loaded config and an already-built
registry. The CLI is responsible for *not* opening the store or constructing
LLM backends on the dry-run path.
"""
from __future__ import annotations

from fabri.core.llm import Provider
from fabri.core.run_config import AgentRunConfig
from fabri.runtime import ROLES, _resolve_role_cfg
from fabri.tools.registry import ToolRegistry


def filter_tools(
    registry: ToolRegistry, search: str | None = None
) -> list[tuple[str, str]]:
    """Return the registry's tools as sorted ``(name, description)`` pairs.

    When `search` is given, keep only tools whose name OR description contains
    it (case-insensitive substring) -- the same loose match a user expects from
    ``grep -i``.
    """
    pairs = sorted(
        (m.name, (m.description or "").strip()) for m in registry.list()
    )
    if not search:
        return pairs
    needle = search.lower()
    return [
        (name, desc)
        for name, desc in pairs
        if needle in name.lower() or needle in desc.lower()
    ]


def render_tools_listing(
    pairs: list[tuple[str, str]], *, search: str | None = None
) -> str:
    """Human-readable listing of ``(name, description)`` pairs."""
    if not pairs:
        if search:
            return f"no tools match {search!r}."
        return "no tools available (check tools.manifest_dir in your config)."

    width = max(len(name) for name, _ in pairs)
    header = (
        f"{len(pairs)} tool(s) matching {search!r}:"
        if search
        else f"{len(pairs)} tool(s) available:"
    )
    lines = [header]
    for name, desc in pairs:
        first_line = desc.splitlines()[0] if desc else ""
        lines.append(f"  {name.ljust(width)}  {first_line}")
    return "\n".join(lines)


def build_dry_run_plan(config: dict, tool_defs: list[dict]) -> dict:
    """Assemble the resolved-run summary for ``fabri run --dry-run``.

    Reports the LLM backend each configured role would use, the memory backend,
    the scalar orchestration knobs (via :class:`AgentRunConfig`), and the exact
    `tool_defs` list that would be handed to the main model. Pure projection of
    an already-loaded config -- no backend is constructed, no store opened.
    """
    roles: dict[str, dict] = {}
    for role in ROLES:
        rcfg = _resolve_role_cfg(config, role)
        if rcfg is None or not rcfg.get("model"):
            continue
        provider = (rcfg.get("provider") or Provider.GEMINI).lower()
        roles[role] = {
            "provider": provider,
            "model": rcfg["model"],
            "max_tokens": int(rcfg.get("max_tokens") or 1024),
            # Bedrock has no api_key_env (creds via the AWS chain); surface the
            # region instead so the user isn't told they need an unrelated key.
            "api_key_env": rcfg.get("api_key_env"),
            "aws_region": rcfg.get("aws_region") if provider == Provider.BEDROCK else None,
        }

    mem = config.get("memory", {})
    run_cfg = AgentRunConfig.from_config(config)
    return {
        "roles": roles,
        "memory": {
            "backend": (mem.get("backend") or "qdrant").lower(),
            "collection": mem.get("collection"),
        },
        "agent": {
            "max_steps": run_cfg.max_steps,
            "output_format": run_cfg.output_format,
            "result_format": run_cfg.result_format,
            "planner_mode": run_cfg.planner_mode,
            "tool_retrieval_enabled": run_cfg.tool_retrieval_enabled,
            "max_cost_usd": run_cfg.max_cost_usd,
        },
        "tool_count": len(tool_defs),
        "tool_defs": tool_defs,
    }


def render_dry_run_plan(plan: dict, *, task: str | None = None) -> str:
    """Render :func:`build_dry_run_plan`'s output as a human-readable plan.

    Ends with the full JSON of the tool definitions that would be sent to the
    model so the user can see exactly what the orchestrator advertises.
    """
    import json

    lines = ["=== fabri run plan (dry run — no LLM call) ==="]
    if task is not None:
        lines.append(f"task: {task!r}")
    lines.append("")

    lines.append("LLM roles:")
    if plan["roles"]:
        width = max(len(r) for r in plan["roles"])
        for role, info in plan["roles"].items():
            if info["provider"] == Provider.BEDROCK:
                cred = f"region={info.get('aws_region') or 'AWS_REGION env'}"
            elif info.get("api_key_env"):
                cred = f"key={info['api_key_env']}"
            else:
                cred = "key=—"
            lines.append(
                f"  {role.ljust(width)}  {info['provider']}  {info['model']}  "
                f"(max_tokens={info['max_tokens']}, {cred})"
            )
    else:
        lines.append("  (none configured)")

    mem = plan["memory"]
    lines.append("")
    lines.append(f"Memory: {mem['backend']} (collection {mem['collection']!r})")

    agent = plan["agent"]
    budget = (
        f"${agent['max_cost_usd']}" if agent["max_cost_usd"] is not None else "none"
    )
    lines.append(
        f"Agent: max_steps={agent['max_steps']}, "
        f"output_format={agent['output_format']}, "
        f"result_format={agent['result_format']}, "
        f"planner={agent['planner_mode']}, "
        f"tool_retrieval={'on' if agent['tool_retrieval_enabled'] else 'off'}, "
        f"max_cost={budget}"
    )

    lines.append("")
    lines.append(f"Tool definitions sent to the model ({plan['tool_count']}):")
    for d in plan["tool_defs"]:
        lines.append(f"  - {d['name']}: {(d.get('description') or '').splitlines()[0] if d.get('description') else ''}")
    lines.append("")
    lines.append(json.dumps(plan["tool_defs"], indent=2))
    return "\n".join(lines)
