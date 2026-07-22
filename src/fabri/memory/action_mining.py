"""Pure mining of deterministic truncation recoveries into ActionMemory."""

from __future__ import annotations

from collections.abc import Mapping

from fabri.orchestrator.action_detection import derive_scope_from_collection


def action_candidate_text(candidate: Mapping[str, object]) -> str:
    """Return stable role-specific text so sibling recoveries cannot collide."""
    scope = candidate.get("scope")
    roles = scope.get("roles") if isinstance(scope, Mapping) else None
    role_names = sorted(role for role in roles or [] if isinstance(role, str) and role)
    role_label = ", ".join(role_names) if role_names else "affected role"
    return f"Increase {role_label} token cap after a truncation retry."


def observed_max_token_retries(
    result: Mapping[str, object],
    post_run_max_token_retries: object = 0,
) -> int:
    """Combine retries from the real run and its memory-compression follow-up."""
    usage = result.get("usage")
    run_value = usage.get("max_token_retries", 0) if isinstance(usage, Mapping) else 0

    def valid_count(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return 0

    return valid_count(run_value) + valid_count(post_run_max_token_retries)


def build_truncation_action_candidate(
    config: dict, max_token_retries: int, outcome: str, task: str
) -> dict | None:
    """Build a typed cap-increase recovery for a truncation retry.

    A positive retry count is the deterministic signal, regardless of the
    terminal outcome: a run that recovered after retrying still proves the
    configured cap was too low. ``outcome`` and ``task`` are intentionally not
    folded into the stable signature, which must remain reusable across runs.
    Missing or malformed configuration fails closed.
    """
    del outcome, task
    if not isinstance(max_token_retries, int) or isinstance(max_token_retries, bool):
        return None
    if max_token_retries <= 0 or not isinstance(config, dict):
        return None

    llm = config.get("llm")
    memory = config.get("memory")
    agent = config.get("agent", {})
    if not isinstance(llm, dict) or not isinstance(memory, dict) or not isinstance(agent, dict):
        return None

    configured_cap = llm.get("max_tokens")
    model_family = llm.get("model")
    collection = memory.get("collection")
    if (
        not isinstance(configured_cap, int)
        or isinstance(configured_cap, bool)
        or configured_cap <= 0
        or not isinstance(model_family, str)
        or not model_family
        or not isinstance(collection, str)
    ):
        return None

    explicit_scope = memory.get("action_scope")
    explicit_role: str | None = None
    if isinstance(explicit_scope, dict):
        company_value = explicit_scope.get("company")
        agency_value = explicit_scope.get("agency")
        role_value = explicit_scope.get("role")
        company = company_value if isinstance(company_value, str) else None
        agency = agency_value if isinstance(agency_value, str) else None
        explicit_role = role_value if isinstance(role_value, str) else None
    else:
        company, agency = derive_scope_from_collection(collection)
    if not company or not agency:
        return None
    roles = _affected_roles(config, configured_cap, explicit_role or agency)
    if not roles:
        return None

    retry_cap = configured_cap * 2
    preconditions = [
        {"field": f"roles_config.{role}.max_tokens", "equals": configured_cap}
        for role in roles
    ]
    steps = [
        {
            "capability": "configure_role",
            "args_template": {"role": role, "max_tokens": retry_cap},
        }
        for role in roles
    ]
    return {
        "problem_signature": {
            "phase": "run",
            "agency": agency,
            "roles": roles,
            "error_class": "LLMError",
            "cause": "truncation",
            "configured_cap": configured_cap,
            "retry_cap": retry_cap,
            "model_family": model_family,
            "finish_reason": "length",
        },
        "scope": {"company": company, "agency": agency, "roles": roles},
        "preconditions": preconditions,
        "steps": steps,
        "postconditions": [
            {"field": f"roles_config.{role}.max_tokens", "equals": retry_cap}
            for role in roles
        ],
        "rollback": {
            "steps": [
                {
                    "capability": "configure_role",
                    "args_template": {"role": role, "max_tokens": configured_cap},
                }
                for role in roles
            ]
        },
        "evidence": {"source_session_ids": [], "verification": "unverified"},
        "policy": {"idempotent": True, "max_attempts": 1, "approval": "shadow"},
    }


def _affected_roles(config: dict, configured_cap: int, fallback_role: str) -> list[str]:
    """Return configured roles at the failed cap, with a stable local fallback."""
    roles: set[str] = set()
    roles_config = config.get("roles_config")
    if isinstance(roles_config, dict):
        for name, role_config in roles_config.items():
            if (
                isinstance(name, str)
                and name
                and isinstance(role_config, dict)
                and role_config.get("max_tokens") == configured_cap
            ):
                roles.add(name)

    tools = config.get("tools")
    agent_entries = tools.get("agents") if isinstance(tools, dict) else None
    if isinstance(agent_entries, list):
        for entry in agent_entries:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and entry["name"]
                and entry.get("max_tokens") == configured_cap
            ):
                roles.add(entry["name"])

    if not roles:
        agent = config.get("agent")
        agent_name = agent.get("name") if isinstance(agent, dict) else None
        if isinstance(agent_name, str) and agent_name:
            roles.add(agent_name)
        else:
            roles.add(fallback_role)
    return sorted(roles)
