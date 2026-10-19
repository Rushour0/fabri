"""Turn a resolved setup into something run_agent can take."""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from fabri.setups.resolver import ResolvedSetup
from fabri.setups.toolset import ResolvedToolSet, resolve_tools
from fabri.tools.catalog import META_TOOLS
from fabri.tools.credential_store import CredentialStore
from fabri.tools.policy import ToolPolicy
from fabri.tools.registry import ToolRegistry


def build_agent_config(
    resolved: ResolvedSetup,
    agent_type: str,
    base_llm_cfg: Mapping[str, Any] | None = None,
    *,
    tool_set: ResolvedToolSet | None = None,
    global_allow: Iterable[str] | None = None,
    policy: ToolPolicy | None = None,
    base_config: Mapping[str, Any] | None = None,
    meta_tools: bool = True,
    sandbox_root: str | None = None,
    transport: httpx.BaseTransport | None = None,
    store: CredentialStore | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], ToolRegistry]:
    """Return ``(config, registry)`` for one agent type.

    `config` is a full fabri config (DEFAULT_CONFIG, then `base_config`, then
    `base_llm_cfg` over ``llm``, then the type's own ``llm`` block from
    agent.yaml). The merged prompt, skills and context go into
    ``agent.system_prompt_prefix``; ``agent.max_steps`` and
    ``agent.max_cost_usd`` come from agent.yaml when set. ``config["setup"]``
    carries the type, the content hash, the loaded skills, the approval
    class of every tool and any budget warnings, for the host.

    `registry` holds the type's tools (agent.yaml `tools` plus its skills'
    tools, or every resolved tool when agent.yaml has no `tools` key), plus
    find_tools and describe_tool when `meta_tools` is true.
    """
    from fabri.config import DEFAULT_CONFIG, _deep_merge, _normalize_llm_roles

    agent = resolved.agent(agent_type)
    if tool_set is None:
        tool_set = resolve_tools(resolved, global_allow=global_allow, policy=policy, transport=transport,
                                 store=store, dry_run=dry_run)
    chosen = tool_set.for_type(resolved, agent_type)
    registry = tool_set.registry([t.name for t in chosen], sandbox_root=sandbox_root,
                                 meta_tools=meta_tools and bool(chosen))
    prompt, warnings = resolved.system_prompt(agent_type)

    cfg = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), dict(base_config or {}))
    cfg["llm"] = _deep_merge(cfg["llm"], dict(base_llm_cfg or {}))
    type_llm = agent["config"].get("llm")
    if isinstance(type_llm, Mapping):
        cfg["llm"] = _deep_merge(cfg["llm"], dict(type_llm))
    cfg["llm"].pop("roles", None)
    agent_cfg = cfg["agent"]
    agent_cfg["name"] = agent_type
    for key in ("max_steps", "max_cost_usd"):
        if agent["config"].get(key) is not None:
            agent_cfg[key] = agent["config"][key]
    existing = agent_cfg.get("system_prompt_prefix") or ""
    agent_cfg["system_prompt_prefix"] = "\n\n".join(p for p in (prompt, existing) if p)
    tools_cfg = cfg["tools"]
    tools_cfg["enabled"] = list(registry.tools)
    retrieval = dict(tools_cfg.get("retrieval") or {})
    retrieval["always_include"] = list(dict.fromkeys([*(retrieval.get("always_include") or []), *META_TOOLS]))
    tools_cfg["retrieval"] = retrieval
    cfg["setup"] = {
        "agent_type": agent_type,
        "content_hash": resolved.content_hash,
        "skills": resolved.skills_for(agent_type),
        "approvals": {n: m.approval for n, m in registry.tools.items()},
        "warnings": [*warnings, *tool_set.errors],
    }
    return _normalize_llm_roles(cfg), registry
