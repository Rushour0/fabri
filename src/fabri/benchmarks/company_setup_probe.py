"""Qualify a roster-company setup before running a memory experiment.

This is deliberately smaller than a general benchmark framework.  It resolves
one declarative dataset case, compiles fresh company copies, recursively
inspects every delegated config, and compares a baseline with a bounded
delegated-main-token-floor candidate.  Only operationally complete runs receive
a rubric verdict.

Usage:
    python -m fabri.benchmarks.company_setup_probe \
        --dataset benchmarks/datasets/company_memory_experiments.yaml \
        --case support_hq_safe_incident_response \
        --output-dir benchmarks/runs/support-hq-setup-probe
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import statistics
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from fabri.config import load_config
from fabri.core.llm import LLMUsage
from fabri.pricing import cost_for

SUCCESS_OUTCOMES = {"success", "success_with_recovery"}
MIN_TOKEN_FLOOR = 64
MAX_TOKEN_FLOOR = 4096
MIN_STEP_BUDGET = 1
MAX_STEP_BUDGET = 200
MAX_COST_CEILING_USD = 10.0
MIN_RETRIEVAL_TOP_K = 1
MAX_RETRIEVAL_TOP_K = 50
MIN_MAX_PARALLEL_SPAWNS = 1
MAX_MAX_PARALLEL_SPAWNS = 64
MIN_DELEGATION_TIMEOUT_S = 1.0
MAX_DELEGATION_TIMEOUT_S = 3600.0
RETRIEVAL_STRATEGIES = {"dense", "sparse", "hybrid", "hybrid+mmr"}
LLM_ROLES = {"main", "decompose", "planner", "narrator"}
DEFAULT_RUN_TIMEOUT_S = 600.0
MANIFEST_GIT_TIMEOUT_S = 15.0
CLAIM_BOUNDARY = "setup qualification only; memory/control result pending"
JUDGE_PROMPT_VERSION = "v1"
JUDGE_TEMPERATURE = 0.0
DEFAULT_JUDGE = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_key_env": "OPENAI_API_KEY",
}
JUDGE_SYSTEM_PROMPT = (
    "You are a frozen semantic evaluator for a benchmark. Apply only the supplied "
    "criteria to the holdout response. Return strict JSON with exactly these fields: "
    '{"verdict":"pass"|"fail","rationale":"brief evidence-based explanation"}. '
    "Do not include markdown or additional fields."
)

CommandRunner = Callable[
    [list[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]


class ProbeError(ValueError):
    """Raised when a setup-probe input or compiled company is invalid."""


@dataclass(frozen=True)
class JudgeConfig:
    """Auditable configuration for the optional semantic judge."""

    provider: str
    model: str
    api_key_env: str
    criteria: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeResult:
    """A judge verdict and the provider usage needed for separate pricing."""

    verdict: str
    rationale: str
    usage: LLMUsage


JudgeRunner = Callable[[JudgeConfig, str], JudgeResult]


@dataclass(frozen=True)
class ProbeCandidate:
    candidate_id: str
    delegated_llm_max_tokens_floor: int | None = None
    step_budget: int | None = None
    retrieval_top_k: int | None = None
    retrieval_strategy: str | None = None
    cost_ceiling: float | None = None
    role_model: tuple[str, str] | None = None
    max_parallel_spawns: int | None = None
    delegation_timeout: float | None = None

    def overrides(self) -> dict[str, object]:
        """Return the public, normalized candidate configuration."""
        overrides: dict[str, object] = {
            "delegated_llm_max_tokens_floor": self.delegated_llm_max_tokens_floor,
        }
        for name in (
            "step_budget",
            "retrieval_top_k",
            "retrieval_strategy",
            "cost_ceiling",
            "max_parallel_spawns",
            "delegation_timeout",
        ):
            value = getattr(self, name)
            if value is not None:
                overrides[name] = value
        if self.role_model is not None:
            overrides["role_model"] = {
                "role": self.role_model[0],
                "model": self.role_model[1],
            }
        return overrides

    def has_overrides(self) -> bool:
        return any(value is not None for value in (
            self.delegated_llm_max_tokens_floor,
            self.step_budget,
            self.retrieval_top_k,
            self.retrieval_strategy,
            self.cost_ceiling,
            self.role_model,
            self.max_parallel_spawns,
            self.delegation_timeout,
        ))


@dataclass(frozen=True)
class ProbeCase:
    case_id: str
    company_source: Path
    company_name: str
    root_id: str
    company_max_cost_usd: float | None
    holdout_prompt: str
    required_terms: tuple[tuple[str, ...], ...]
    forbidden_terms: tuple[str, ...]
    required_delegations: tuple[str, ...]
    replicas: int
    candidates: tuple[ProbeCandidate, ...]
    structured_fields: Mapping[str, object] = field(default_factory=dict)
    judge: JudgeConfig | None = None


@dataclass(frozen=True)
class ConfigLocation:
    path: Path
    org_path: str
    timeout_s: float | None


def _as_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbeError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _as_string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProbeError(f"{field} must be a list of strings")
    return tuple(value)


def _as_required_groups(value: object, field: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ProbeError(f"{field} must be a list")
    groups: list[tuple[str, ...]] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item:
            groups.append((item,))
        elif (
            isinstance(item, list)
            and item
            and all(isinstance(phrase, str) and phrase for phrase in item)
        ):
            groups.append(tuple(item))
        else:
            raise ProbeError(
                f"{field}[{index}] must be a string or non-empty list of strings"
            )
    return tuple(groups)


def _load_judge_config(value: object, field: str) -> JudgeConfig:
    """Validate an opt-in dataset judge block, applying frozen defaults."""
    judge = _as_mapping(value, field)
    unknown = set(judge) - {"provider", "model", "api_key_env", "criteria"}
    if unknown:
        raise ProbeError(f"{field} contains unsupported settings: " + ", ".join(sorted(unknown)))
    provider = judge.get("provider", DEFAULT_JUDGE["provider"])
    model = judge.get("model", DEFAULT_JUDGE["model"])
    api_key_env = judge.get("api_key_env", DEFAULT_JUDGE["api_key_env"])
    for key, item in {
        "provider": provider,
        "model": model,
        "api_key_env": api_key_env,
    }.items():
        if not isinstance(item, str) or not item:
            raise ProbeError(f"{field}.{key} must be a non-empty string")
    return JudgeConfig(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        criteria=_as_string_list(judge.get("criteria", []), f"{field}.criteria"),
    )


def _default_judge_config() -> JudgeConfig:
    return JudgeConfig(
        provider=DEFAULT_JUDGE["provider"],
        model=DEFAULT_JUDGE["model"],
        api_key_env=DEFAULT_JUDGE["api_key_env"],
    )


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProbeError(f"{field} must be a positive integer")
    return value


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ProbeError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _bounded_float(value: object, field: str, minimum: float, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not minimum <= float(value) <= maximum
    ):
        raise ProbeError(f"{field} must be a number between {minimum:g} and {maximum:g}")
    return float(value)


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProbeError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ProbeError(f"malformed YAML in {path}: {exc}") from exc
    return _as_mapping(loaded, str(path))


def load_probe_case(
    dataset_path: str | Path,
    case_id: str,
    *,
    replicas_override: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProbeCase:
    """Load and validate one case, resolving its source through the dataset env."""
    dataset = _load_yaml_mapping(Path(dataset_path))
    defaults = _as_mapping(dataset.get("defaults"), "defaults")
    roster_root_env = defaults.get("roster_root_env")
    if not isinstance(roster_root_env, str) or not roster_root_env:
        raise ProbeError("defaults.roster_root_env must be a non-empty string")
    environment = os.environ if environ is None else environ
    roster_root = environment.get(roster_root_env)
    if not roster_root:
        raise ProbeError(f"{roster_root_env} is not set")

    cases = dataset.get("cases")
    if not isinstance(cases, list):
        raise ProbeError("cases must be a list")
    selected: dict[str, object] | None = None
    for index, raw_case in enumerate(cases):
        case = _as_mapping(raw_case, f"cases[{index}]")
        if case.get("id") == case_id:
            selected = case
            break
    if selected is None:
        raise ProbeError(f"case not found: {case_id}")

    source_value = selected.get("company_source")
    if not isinstance(source_value, str) or not source_value:
        raise ProbeError(f"case {case_id}.company_source must be a non-empty string")
    company_source = (Path(roster_root) / source_value).resolve()
    if not company_source.is_file():
        raise ProbeError(f"company source not found for {case_id}: {company_source}")
    try:
        company_data = tomllib.loads(company_source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProbeError(f"could not read company source {company_source}: {exc}") from exc
    company = _as_mapping(company_data.get("company"), "company")
    company_name = company.get("name")
    if not isinstance(company_name, str) or not company_name:
        raise ProbeError("company.name must be a non-empty string")
    nodes = company_data.get("node")
    if not isinstance(nodes, list):
        raise ProbeError("company nodes must be a list")
    root_ids = [
        node.get("id")
        for node in nodes
        if isinstance(node, dict) and node.get("report_to") == ""
    ]
    if len(root_ids) != 1 or not isinstance(root_ids[0], str):
        raise ProbeError("company must contain exactly one root node")
    max_cost_value = company.get("max_cost_usd")
    if max_cost_value is not None and not isinstance(max_cost_value, (int, float)):
        raise ProbeError("company.max_cost_usd must be numeric")

    holdout_prompt = selected.get("holdout_prompt")
    if not isinstance(holdout_prompt, str) or not holdout_prompt:
        raise ProbeError(f"case {case_id}.holdout_prompt must be a non-empty string")
    expected = _as_mapping(selected.get("expected"), f"case {case_id}.expected")
    setup = _as_mapping(selected.get("setup_probe"), f"case {case_id}.setup_probe")
    required_delegations = _as_string_list(
        setup.get("required_delegations"),
        f"case {case_id}.setup_probe.required_delegations",
    )

    raw_candidates = setup.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ProbeError(f"case {case_id}.setup_probe.candidates must be a non-empty list")
    candidates: list[ProbeCandidate] = []
    seen_ids: set[str] = set()
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _as_mapping(raw_candidate, f"setup_probe.candidates[{index}]")
        candidate_name = candidate.get("id")
        if not isinstance(candidate_name, str) or not candidate_name:
            raise ProbeError(f"setup_probe.candidates[{index}].id must be a string")
        if candidate_name in seen_ids:
            raise ProbeError(f"duplicate setup candidate id: {candidate_name}")
        seen_ids.add(candidate_name)
        floor_value = candidate.get("delegated_llm_max_tokens_floor")
        floor = (
            None
            if floor_value is None
            else _bounded_int(
                floor_value,
                f"candidate {candidate_name} token floor",
                MIN_TOKEN_FLOOR,
                MAX_TOKEN_FLOOR,
            )
        )
        step_budget_value = candidate.get("step_budget")
        step_budget = (
            None
            if step_budget_value is None
            else _bounded_int(
                step_budget_value,
                f"candidate {candidate_name} step_budget",
                MIN_STEP_BUDGET,
                MAX_STEP_BUDGET,
            )
        )
        retrieval_top_k_value = candidate.get("retrieval_top_k")
        retrieval_top_k = (
            None
            if retrieval_top_k_value is None
            else _bounded_int(
                retrieval_top_k_value,
                f"candidate {candidate_name} retrieval_top_k",
                MIN_RETRIEVAL_TOP_K,
                MAX_RETRIEVAL_TOP_K,
            )
        )
        retrieval_strategy_value = candidate.get("retrieval_strategy")
        if retrieval_strategy_value is not None and (
            not isinstance(retrieval_strategy_value, str)
            or retrieval_strategy_value not in RETRIEVAL_STRATEGIES
        ):
            raise ProbeError(
                f"candidate {candidate_name} retrieval_strategy must be one of "
                + ", ".join(sorted(RETRIEVAL_STRATEGIES))
            )
        cost_ceiling_value = candidate.get("cost_ceiling")
        if cost_ceiling_value is None:
            cost_ceiling = None
        elif (
            not isinstance(cost_ceiling_value, (int, float))
            or isinstance(cost_ceiling_value, bool)
            or not 0 < float(cost_ceiling_value) <= MAX_COST_CEILING_USD
        ):
            raise ProbeError(
                f"candidate {candidate_name} cost_ceiling must be between 0 and "
                f"{MAX_COST_CEILING_USD:g}"
            )
        else:
            cost_ceiling = float(cost_ceiling_value)
        role_model_value = candidate.get("role_model")
        if role_model_value is None:
            role_model = None
        else:
            role_model_map = _as_mapping(
                role_model_value, f"candidate {candidate_name} role_model"
            )
            if set(role_model_map) != {"role", "model"}:
                raise ProbeError(
                    f"candidate {candidate_name} role_model needs exactly role and model"
                )
            role = role_model_map.get("role")
            model = role_model_map.get("model")
            if not isinstance(role, str) or role not in LLM_ROLES:
                raise ProbeError(
                    f"candidate {candidate_name} role_model.role must be one of "
                    + ", ".join(sorted(LLM_ROLES))
                )
            if not isinstance(model, str) or not model:
                raise ProbeError(f"candidate {candidate_name} role_model.model must be a string")
            role_model = (role, model)
        max_parallel_spawns_value = candidate.get("max_parallel_spawns")
        max_parallel_spawns = (
            None
            if max_parallel_spawns_value is None
            else _bounded_int(
                max_parallel_spawns_value,
                f"candidate {candidate_name} max_parallel_spawns",
                MIN_MAX_PARALLEL_SPAWNS,
                MAX_MAX_PARALLEL_SPAWNS,
            )
        )
        delegation_timeout_value = candidate.get("delegation_timeout")
        delegation_timeout = (
            None
            if delegation_timeout_value is None
            else _bounded_float(
                delegation_timeout_value,
                f"candidate {candidate_name} delegation_timeout",
                MIN_DELEGATION_TIMEOUT_S,
                MAX_DELEGATION_TIMEOUT_S,
            )
        )
        unknown = set(candidate) - {
            "id",
            "delegated_llm_max_tokens_floor",
            "step_budget",
            "retrieval_top_k",
            "retrieval_strategy",
            "cost_ceiling",
            "role_model",
            "max_parallel_spawns",
            "delegation_timeout",
        }
        if unknown:
            raise ProbeError(
                f"candidate {candidate_name} contains unsupported settings: "
                + ", ".join(sorted(unknown))
            )
        candidates.append(
            ProbeCandidate(
                candidate_id=candidate_name,
                delegated_llm_max_tokens_floor=floor,
                step_budget=step_budget,
                retrieval_top_k=retrieval_top_k,
                retrieval_strategy=retrieval_strategy_value,
                cost_ceiling=cost_ceiling,
                role_model=role_model,
                max_parallel_spawns=max_parallel_spawns,
                delegation_timeout=delegation_timeout,
            )
        )

    replicas_value = setup.get("replicas", defaults.get("setup_probe_replicas", 3))
    replicas = (
        _positive_int(replicas_override, "--replicas")
        if replicas_override is not None
        else _positive_int(replicas_value, f"case {case_id}.setup_probe.replicas")
    )
    return ProbeCase(
        case_id=case_id,
        company_source=company_source,
        company_name=company_name,
        root_id=root_ids[0],
        company_max_cost_usd=float(max_cost_value) if max_cost_value is not None else None,
        holdout_prompt=holdout_prompt,
        required_terms=_as_required_groups(expected.get("required"), "expected.required"),
        forbidden_terms=_as_string_list(expected.get("forbidden"), "expected.forbidden"),
        required_delegations=required_delegations,
        replicas=replicas,
        candidates=tuple(candidates),
        structured_fields=_as_mapping(
            expected.get("structured", {}), f"case {case_id}.expected.structured"
        ),
        judge=(
            _load_judge_config(selected["judge"], f"case {case_id}.judge")
            if "judge" in selected
            else None
        ),
    )


def discover_company_configs(root_config: Path) -> list[ConfigLocation]:
    """Recursively follow static agent-tool config edges from a compiled root."""
    queue = [ConfigLocation(root_config.resolve(), "root", None)]
    found: list[ConfigLocation] = []
    seen: set[Path] = set()
    while queue:
        location = queue.pop(0)
        if location.path in seen:
            continue
        seen.add(location.path)
        data = _load_yaml_mapping(location.path)
        found.append(location)
        tools = data.get("tools")
        if tools is None:
            continue
        tools_map = _as_mapping(tools, f"{location.path}.tools")
        agents = tools_map.get("agents", [])
        if not isinstance(agents, list):
            raise ProbeError(f"{location.path}.tools.agents must be a list")
        for index, raw_agent in enumerate(agents):
            agent = _as_mapping(raw_agent, f"{location.path}.tools.agents[{index}]")
            name = agent.get("name")
            config_value = agent.get("config")
            if not isinstance(name, str) or not isinstance(config_value, str):
                raise ProbeError(f"{location.path}.tools.agents[{index}] needs name and config")
            child_path = Path(config_value)
            if not child_path.is_absolute():
                child_path = location.path.parent / child_path
            timeout = agent.get("timeout_s", 120.0)
            if not isinstance(timeout, (int, float)):
                raise ProbeError(f"{location.path} delegation {name} timeout_s must be numeric")
            queue.append(
                ConfigLocation(child_path.resolve(), f"{location.org_path}/{name}", float(timeout))
            )
    return found


def apply_delegated_token_floor(
    root_config: Path,
    floor: int | None,
) -> list[str]:
    """Raise low delegated main-role ceilings without touching root or narration."""
    if floor is None:
        return []
    if not MIN_TOKEN_FLOOR <= floor <= MAX_TOKEN_FLOOR:
        raise ProbeError(f"token floor must be between {MIN_TOKEN_FLOOR} and {MAX_TOKEN_FLOOR}")
    changed: list[str] = []
    for location in discover_company_configs(root_config):
        if location.path == root_config.resolve():
            continue
        effective = load_config(str(location.path))
        roles = effective.get("llm", {}).get("roles", {})
        main = roles.get("main") if isinstance(roles, dict) else None
        current = main.get("max_tokens") if isinstance(main, dict) else None
        if not isinstance(current, int) or current >= floor:
            continue
        raw = _load_yaml_mapping(location.path)
        llm_value = raw.setdefault("llm", {})
        llm = _as_mapping(llm_value, f"{location.path}.llm")
        llm["max_tokens"] = floor
        raw["llm"] = llm
        location.path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        changed.append(location.org_path)
    return changed


@dataclass(frozen=True)
class DelegationEdge:
    parent_path: Path
    agent_index: int
    child_path: Path
    child_org_path: str


def _delegation_edges_by_child(root_config: Path) -> dict[Path, list[DelegationEdge]]:
    """Re-walk raw config edges so timeout edits land in their parent files."""
    queue = [(root_config.resolve(), "root")]
    seen: set[Path] = set()
    edges_by_child: dict[Path, list[DelegationEdge]] = {}
    while queue:
        parent_path, parent_org_path = queue.pop(0)
        if parent_path in seen:
            continue
        seen.add(parent_path)
        data = _load_yaml_mapping(parent_path)
        tools = data.get("tools")
        if tools is None:
            continue
        agents = _as_mapping(tools, f"{parent_path}.tools").get("agents", [])
        if not isinstance(agents, list):
            raise ProbeError(f"{parent_path}.tools.agents must be a list")
        for index, raw_agent in enumerate(agents):
            agent = _as_mapping(raw_agent, f"{parent_path}.tools.agents[{index}]")
            name = agent.get("name")
            config_value = agent.get("config")
            if not isinstance(name, str) or not isinstance(config_value, str):
                raise ProbeError(f"{parent_path}.tools.agents[{index}] needs name and config")
            child_path = Path(config_value)
            if not child_path.is_absolute():
                child_path = parent_path.parent / child_path
            child_path = child_path.resolve()
            child_org_path = f"{parent_org_path}/{name}"
            edge = DelegationEdge(parent_path, index, child_path, child_org_path)
            edges_by_child.setdefault(child_path, []).append(edge)
            queue.append((child_path, child_org_path))
    return edges_by_child


def _write_yaml_mapping(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _mark_changed(changed: list[str], org_path: str) -> None:
    if org_path not in changed:
        changed.append(org_path)


def _effective_agent_budget(
    agent: object,
    field: str,
    default: int | float | None,
) -> int | float | None:
    """Return the budget a sub-agent actually receives at runtime."""
    if not isinstance(agent, dict):
        return default
    subagent = agent.get("subagent")
    subagent_value = subagent.get(field) if isinstance(subagent, dict) else None
    if subagent_value is not None:
        return subagent_value if isinstance(subagent_value, (int, float)) else default
    value = agent.get(field)
    return value if isinstance(value, (int, float)) else default


def _write_agent_budget(
    raw_agent: dict[str, object],
    field: str,
    value: int | float,
    *,
    path: Path,
) -> None:
    """Update a budget and its declared sub-agent override, if any."""
    raw_agent[field] = value
    # Mirror into a declared sub-agent block so the runtime-effective budget
    # (subagent.<field> or agent.<field>) actually changes. A bare `subagent:`
    # (YAML null) has no override to shadow the write, so treat it as absent —
    # matching the read side in _effective_agent_budget.
    if not isinstance(raw_agent.get("subagent"), dict):
        return
    subagent = _as_mapping(raw_agent["subagent"], f"{path}.agent.subagent")
    subagent[field] = value
    raw_agent["subagent"] = subagent


def _validate_candidate_overrides(candidate: ProbeCandidate) -> None:
    if candidate.delegated_llm_max_tokens_floor is not None:
        _bounded_int(
            candidate.delegated_llm_max_tokens_floor,
            "token floor",
            MIN_TOKEN_FLOOR,
            MAX_TOKEN_FLOOR,
        )
    if candidate.step_budget is not None:
        _bounded_int(candidate.step_budget, "step_budget", MIN_STEP_BUDGET, MAX_STEP_BUDGET)
    if candidate.retrieval_top_k is not None:
        _bounded_int(
            candidate.retrieval_top_k,
            "retrieval_top_k",
            MIN_RETRIEVAL_TOP_K,
            MAX_RETRIEVAL_TOP_K,
        )
    if candidate.retrieval_strategy is not None and (
        not isinstance(candidate.retrieval_strategy, str)
        or candidate.retrieval_strategy not in RETRIEVAL_STRATEGIES
    ):
        raise ProbeError("retrieval_strategy must be an allowed strategy")
    if candidate.cost_ceiling is not None:
        if (
            not isinstance(candidate.cost_ceiling, (int, float))
            or isinstance(candidate.cost_ceiling, bool)
            or not 0 < candidate.cost_ceiling <= MAX_COST_CEILING_USD
        ):
            raise ProbeError(
                f"cost_ceiling must be between 0 and {MAX_COST_CEILING_USD:g}"
            )
    if candidate.role_model is not None:
        role, model = candidate.role_model
        if role not in LLM_ROLES or not model:
            raise ProbeError("role_model must have an allowed role and non-empty model")
    if candidate.max_parallel_spawns is not None:
        _bounded_int(
            candidate.max_parallel_spawns,
            "max_parallel_spawns",
            MIN_MAX_PARALLEL_SPAWNS,
            MAX_MAX_PARALLEL_SPAWNS,
        )
    if candidate.delegation_timeout is not None:
        _bounded_float(
            candidate.delegation_timeout,
            "delegation_timeout",
            MIN_DELEGATION_TIMEOUT_S,
            MAX_DELEGATION_TIMEOUT_S,
        )


def apply_candidate(root_config: Path, candidate: ProbeCandidate) -> list[str]:
    """Apply a bounded candidate to compiled nodes, spawners, and delegation edges."""
    _validate_candidate_overrides(candidate)
    changed = apply_delegated_token_floor(
        root_config, candidate.delegated_llm_max_tokens_floor
    )
    root_path = root_config.resolve()
    for location in discover_company_configs(root_config):
        if location.path == root_path:
            continue
        effective = load_config(str(location.path))
        agent = effective.get("agent", {})
        memory = effective.get("memory", {})
        llm = effective.get("llm", {})
        raw: dict[str, object] | None = None

        def raw_section(name: str) -> dict[str, object]:
            nonlocal raw
            if raw is None:
                raw = _load_yaml_mapping(location.path)
            section = _as_mapping(raw.setdefault(name, {}), f"{location.path}.{name}")
            raw[name] = section
            return section

        if candidate.step_budget is not None:
            current = _effective_agent_budget(agent, "max_steps", 10)
            if not isinstance(current, int) or current < candidate.step_budget:
                _write_agent_budget(
                    raw_section("agent"),
                    "max_steps",
                    candidate.step_budget,
                    path=location.path,
                )
        if candidate.retrieval_top_k is not None:
            current = memory.get("top_k") if isinstance(memory, dict) else None
            if not isinstance(current, int) or current < candidate.retrieval_top_k:
                raw_section("memory")["top_k"] = candidate.retrieval_top_k
        if candidate.retrieval_strategy is not None:
            current = memory.get("retrieval_strategy") if isinstance(memory, dict) else None
            if current != candidate.retrieval_strategy:
                raw_section("memory")["retrieval_strategy"] = candidate.retrieval_strategy
        if candidate.cost_ceiling is not None:
            current = _effective_agent_budget(agent, "max_cost_usd", None)
            if current is None or not isinstance(current, (int, float)) or current > candidate.cost_ceiling:
                _write_agent_budget(
                    raw_section("agent"),
                    "max_cost_usd",
                    candidate.cost_ceiling,
                    path=location.path,
                )
        if candidate.role_model is not None:
            role, target_model = candidate.role_model
            roles = llm.get("roles", {}) if isinstance(llm, dict) else {}
            effective_role = roles.get(role) if isinstance(roles, dict) else None
            current = effective_role.get("model") if isinstance(effective_role, dict) else None
            if current != target_model:
                raw_llm = raw_section("llm")
                # A model on default-disabled planner/decompose enables that role.
                if role == "main":
                    raw_llm["model"] = target_model
                else:
                    role_value = raw_llm.get(role)
                    role_config = (
                        _as_mapping(role_value, f"{location.path}.llm.{role}")
                        if isinstance(role_value, dict)
                        else {}
                    )
                    role_config["model"] = target_model
                    raw_llm[role] = role_config
        if raw is not None:
            _write_yaml_mapping(location.path, raw)
            _mark_changed(changed, location.org_path)

    if candidate.max_parallel_spawns is not None:
        for location in discover_company_configs(root_config):
            raw = _load_yaml_mapping(location.path)
            raw_tools = raw.get("tools")
            if not isinstance(raw_tools, dict):
                continue
            raw_agents = raw_tools.get("agents", [])
            if not isinstance(raw_agents, list) or not raw_agents:
                continue
            effective = load_config(str(location.path))
            tools = effective.get("tools", {})
            current = tools.get("max_parallel_spawns", 4) if isinstance(tools, dict) else 4
            if isinstance(current, int) and current >= candidate.max_parallel_spawns:
                continue
            tools = _as_mapping(raw_tools, f"{location.path}.tools")
            tools["max_parallel_spawns"] = candidate.max_parallel_spawns
            raw["tools"] = tools
            _write_yaml_mapping(location.path, raw)
            _mark_changed(changed, location.org_path)

    if candidate.delegation_timeout is not None:
        for child_edges in _delegation_edges_by_child(root_config).values():
            for edge in child_edges:
                effective_parent = load_config(str(edge.parent_path))
                effective_tools = effective_parent.get("tools", {})
                effective_agents = (
                    effective_tools.get("agents", [])
                    if isinstance(effective_tools, dict)
                    else []
                )
                effective_agent = (
                    effective_agents[edge.agent_index]
                    if isinstance(effective_agents, list)
                    and edge.agent_index < len(effective_agents)
                    else None
                )
                current = (
                    effective_agent.get("timeout_s", 120.0)
                    if isinstance(effective_agent, dict)
                    else 120.0
                )
                if isinstance(current, (int, float)) and current >= candidate.delegation_timeout:
                    continue
                parent = _load_yaml_mapping(edge.parent_path)
                tools = _as_mapping(
                    parent.setdefault("tools", {}), f"{edge.parent_path}.tools"
                )
                agents = tools.get("agents", [])
                if not isinstance(agents, list):
                    raise ProbeError(f"{edge.parent_path}.tools.agents must be a list")
                agent = _as_mapping(
                    agents[edge.agent_index],
                    f"{edge.parent_path}.tools.agents[{edge.agent_index}]",
                )
                agent["timeout_s"] = candidate.delegation_timeout
                agents[edge.agent_index] = agent
                tools["agents"] = agents
                parent["tools"] = tools
                _write_yaml_mapping(edge.parent_path, parent)
                _mark_changed(changed, edge.child_org_path)
    return changed


def build_preflight_manifest(root_config: Path) -> dict[str, object]:
    """Return the effective recursive company setup and actionable warnings."""
    configs: list[dict[str, object]] = []
    warnings: list[str] = []
    memory_owners: dict[tuple[str, str], str] = {}
    for location in discover_company_configs(root_config):
        config = load_config(str(location.path))
        agent = config.get("agent", {})
        llm = config.get("llm", {})
        roles = llm.get("roles", {}) if isinstance(llm, dict) else {}
        role_rows: list[dict[str, object]] = []
        if isinstance(roles, dict):
            for role_name, raw_role in roles.items():
                if not isinstance(raw_role, dict):
                    continue
                max_tokens = raw_role.get("max_tokens")
                role_rows.append({
                    "role": role_name,
                    "artifact_role": role_name == "main",
                    "provider": raw_role.get("provider"),
                    "model": raw_role.get("model"),
                    "max_tokens": max_tokens,
                    "api_key_env": raw_role.get("api_key_env"),
                    "credential_present": bool(
                        raw_role.get("api_key_env")
                        and os.environ.get(str(raw_role.get("api_key_env")))
                    ),
                })
                if role_name == "main" and isinstance(max_tokens, int) and max_tokens < 256:
                    warnings.append(
                        f"{location.org_path}: artifact-producing main role has "
                        f"max_tokens={max_tokens}"
                    )
        memory = config.get("memory", {})
        memory_map = memory if isinstance(memory, dict) else {}
        memory_key = (
            str(memory_map.get("sqlite_path") or memory_map.get("qdrant_url") or ""),
            str(memory_map.get("collection") or ""),
        )
        previous_owner = memory_owners.get(memory_key)
        if memory_key != ("", "") and previous_owner is not None:
            warnings.append(
                f"memory collision: {previous_owner} and {location.org_path} share "
                f"path/collection {memory_key[1]!r}"
            )
        memory_owners[memory_key] = location.org_path
        tools = config.get("tools", {})
        tools_map = tools if isinstance(tools, dict) else {}
        configs.append({
            "org_path": location.org_path,
            "config_path": str(location.path),
            "delegation_timeout_s": location.timeout_s,
            "max_steps": agent.get("max_steps") if isinstance(agent, dict) else None,
            "max_cost_usd": agent.get("max_cost_usd") if isinstance(agent, dict) else None,
            "roles": role_rows,
            "enabled_tools": tools_map.get("enabled"),
            "sandbox_root": tools_map.get("sandbox_root"),
            "memory": {
                "backend": memory_map.get("backend"),
                "path": memory_key[0],
                "collection": memory_key[1],
                "top_k": memory_map.get("top_k"),
            },
        })
    return {"configs": configs, "warnings": warnings}


def score_text(
    text: str,
    required_terms: tuple[tuple[str, ...], ...],
    forbidden_terms: tuple[str, ...],
) -> dict[str, object]:
    morphological_families = (
        frozenset({
            "verify",
            "verifies",
            "verified",
            "verifying",
            "verification",
            "verifications",
        }),
    )
    denial_evidence_words = {
        "available",
        "completed",
        "confirmed",
        "demonstrated",
        "documented",
        "established",
        "evidenced",
        "executed",
        "found",
        "known",
        "observed",
        "performed",
        "proven",
        "provided",
        "shown",
        "supplied",
        "verified",
    }
    denial_status_words = {
        "absent",
        "incomplete",
        "missing",
        "unavailable",
        "unconfirmed",
        "undocumented",
        "unproven",
        "unsupported",
        "unverified",
    }
    status_label_words = {"action", "completion", "evidence", "fact", "facts", "status"}
    polarity_neutral_requirements = {"evidence"}

    def normalize(value: str) -> str:
        return " ".join(value.casefold().replace("-", " ").split())

    def word_matches(expected: str, actual: str) -> bool:
        if actual == expected or actual == f"{expected}s" or actual == f"{expected}es":
            return True
        return any(expected in family and actual in family for family in morphological_families)

    negation_pattern = re.compile(
        r"\b(no|not(?!\s+only\b)|never|without|nor|neither|cannot|can not|n't|"
        r"did not|does not|do not|isn't|wasn't|aren't|weren't|absence of|lack of|"
        r"unable|no evidence)\b",
        re.IGNORECASE,
    )
    negation_scope_reset_pattern = re.compile(
        r"\b(?:but|however|nevertheless|yet)\b",
        re.IGNORECASE,
    )

    normalized = re.sub(r"[^\S\n]+", " ", text.casefold().replace("-", " "))

    def is_negated(start: int, end: int) -> bool:
        preceding = preceding_clause(start)
        if negation_pattern.search(preceding):
            return True

        following_words = following_clause_words(end)
        if not following_words:
            return False
        if following_words[0] in status_label_words and any(
            word in {"no", "not", "never"} for word in following_words[1:4]
        ):
            return True
        if any(word in denial_status_words for word in following_words[:4]):
            return True
        for index, word in enumerate(following_words[:5]):
            if word == "not" and following_words[index + 1:index + 2] == ["only"]:
                continue
            if word in {"not", "never", "cannot"} and any(
                candidate in denial_evidence_words
                for candidate in following_words[index + 1:index + 5]
            ):
                return True
        return False

    def preceding_clause(start: int) -> str:
        preceding = normalized[max(0, start - 400):start]
        boundary = max(
            preceding.rfind("."),
            preceding.rfind(";"),
            preceding.rfind("!"),
            preceding.rfind("?"),
            preceding.rfind("\n"),
        )
        if boundary != -1:
            preceding = preceding[boundary + 1:]
        scope_resets = list(negation_scope_reset_pattern.finditer(preceding))
        if scope_resets:
            preceding = preceding[scope_resets[-1].end():]
        return preceding

    def following_clause_words(end: int) -> list[str]:
        following = normalized[end:]
        following_boundary = min(
            (
                index
                for punctuation in ".;!?\n"
                if (index := following.find(punctuation)) != -1
            ),
            default=len(following),
        )
        return re.findall(r"\b\w+\b", following[:following_boundary])[:8]

    def required_occurrence_is_denied(start: int, end: int) -> bool:
        if is_negated(start, end):
            return True
        if re.search(r"\buntil\b", preceding_clause(start)):
            return True
        return "investigation" in following_clause_words(end)[:4]

    def required_occurrences(phrase: str) -> list[tuple[int, int]]:
        words = normalize(phrase).split()
        if not words:
            return []

        occurrences: list[tuple[int, int]] = []
        for sentence_match in re.finditer(r"[^.!?\n]+", normalized):
            sentence = sentence_match.group()
            tokens = list(re.finditer(r"\b\w+\b", sentence))
            if len(words) == 1:
                occurrences.extend(
                    (
                        sentence_match.start() + token.start(),
                        sentence_match.start() + token.end(),
                    )
                    for token in tokens
                    if word_matches(words[0], token.group())
                )
                continue

            # A hyphenated requirement may also be written as one compound word.
            # Token comparison also covers hyphenated and space-separated forms.
            compact = "".join(words)
            if "-" in phrase:
                occurrences.extend(
                    (
                        sentence_match.start() + token.start(),
                        sentence_match.start() + token.end(),
                    )
                    for token in tokens
                    if word_matches(compact, token.group())
                )

            for start_index, token in enumerate(tokens):
                if not word_matches(words[0], token.group()):
                    continue
                previous = start_index
                for word in words[1:]:
                    upper_bound = min(previous + 5, len(tokens))
                    next_index = next(
                        (
                            index
                            for index in range(previous + 1, upper_bound)
                            if word_matches(word, tokens[index].group())
                        ),
                        None,
                    )
                    if next_index is None:
                        break
                    previous = next_index
                else:
                    occurrences.append((
                        sentence_match.start() + token.start(),
                        sentence_match.start() + tokens[previous].end(),
                    ))
        return occurrences

    def required_term_matches(phrase: str) -> bool:
        """Match an affirmative requirement within a short, single-sentence window."""
        if normalize(phrase) in polarity_neutral_requirements:
            return bool(required_occurrences(phrase))
        return any(
            not required_occurrence_is_denied(start, end)
            for start, end in required_occurrences(phrase)
        )

    missing = [
        " | ".join(group)
        for group in required_terms
        if not any(required_term_matches(phrase) for phrase in group)
    ]
    forbidden = []
    for term in forbidden_terms:
        normalized_term = normalize(term)
        term_pattern = re.compile(
            r"(?<!\w)"
            + re.escape(normalized_term).replace(r"\ ", r"[ \n]+")
            + r"(?!\w)"
        )
        match = term_pattern.search(normalized)
        while match is not None:
            if not is_negated(match.start(), match.end()):
                forbidden.append(term)
                break
            match = term_pattern.search(normalized, match.start() + 1)
    return {"passed": not missing and not forbidden, "missing": missing, "forbidden": forbidden}


def score_structured(
    structured_output: object,
    structured_fields: Mapping[str, object],
) -> dict[str, object]:
    """Score required structured fields by deterministic recursive subset equality."""
    if not structured_fields:
        return {"passed": True, "mismatches": []}
    if not isinstance(structured_output, dict):
        return {"passed": False, "mismatches": ["structured_output:not_a_mapping"]}

    mismatches: list[str] = []

    def compare(actual: dict[str, object], expected: Mapping[str, object], prefix: str) -> None:
        for key, expected_value in expected.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in actual:
                mismatches.append(f"missing:{path}")
                continue
            actual_value = actual[key]
            if isinstance(expected_value, Mapping):
                if not isinstance(actual_value, dict):
                    mismatches.append(f"wrong:{path}")
                else:
                    compare(actual_value, expected_value, path)
            # Guard Python's True == 1 / False == 0: a wrong-typed structured
            # value (e.g. JSON number 1 against an expected bool true) must not
            # false-pass, since structured only ever tightens qualification.
            elif isinstance(expected_value, bool) != isinstance(actual_value, bool) \
                    or actual_value != expected_value:
                mismatches.append(f"wrong:{path}")

    compare(structured_output, structured_fields, "")
    return {"passed": not mismatches, "mismatches": mismatches}


def _judge_user_prompt(config: JudgeConfig, final_text: str) -> str:
    return json.dumps(
        {
            "prompt_version": JUDGE_PROMPT_VERSION,
            "criteria": list(config.criteria),
            "holdout_final_text": final_text,
        },
        ensure_ascii=False,
    )


def _judge_verdict(value: object) -> tuple[str, str]:
    response = _as_mapping(value, "judge response")
    if set(response) != {"verdict", "rationale"}:
        raise ProbeError("judge response must contain exactly verdict and rationale")
    verdict = response.get("verdict")
    rationale = response.get("rationale")
    if verdict not in {"pass", "fail"} or not isinstance(rationale, str):
        raise ProbeError("judge response has an invalid verdict or rationale")
    return verdict, rationale


def _run_default_judge(config: JudgeConfig, final_text: str) -> JudgeResult:
    """Call a provider SDK directly so the frozen temperature is explicit."""
    user_prompt = _judge_user_prompt(config, final_text)
    if config.provider == "openai":
        import openai

        client = openai.OpenAI(api_key=os.environ.get(config.api_key_env))
        response = client.chat.completions.create(
            model=config.model,
            temperature=JUDGE_TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise ProbeError("judge returned no text")
        verdict, rationale = _judge_verdict(json.loads(content))
        usage = response.usage
        return JudgeResult(
            verdict=verdict,
            rationale=rationale,
            usage=LLMUsage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                model=config.model,
            ),
        )
    if config.provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ.get(config.api_key_env))
        response = client.messages.create(
            model=config.model,
            max_tokens=512,
            temperature=JUDGE_TEMPERATURE,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_blocks = [block.text for block in response.content if block.type == "text"]
        if not text_blocks:
            raise ProbeError("judge returned no text")
        verdict, rationale = _judge_verdict(json.loads("".join(text_blocks)))
        usage = response.usage
        return JudgeResult(
            verdict=verdict,
            rationale=rationale,
            usage=LLMUsage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                ),
                cache_read_input_tokens=int(
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                ),
                model=config.model,
            ),
        )
    raise ProbeError(f"unsupported judge provider: {config.provider}")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if not path.is_file():
        return events
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"malformed trace {path} line {line_number}: {exc}") from exc
        events.append(_as_mapping(raw, f"{path}:{line_number}"))
    return events


def _failure_strings(value: object, *, key: str = "") -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            strings.extend(_failure_strings(child, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_failure_strings(child, key=key))
    elif isinstance(value, str) and key in {"error", "reason", "stderr", "stderr_tail"}:
        strings.append(value)
    return strings


def analyze_run(
    payload: dict[str, object],
    state_root: Path,
    required_delegations: tuple[str, ...],
) -> dict[str, object]:
    """Recursively validate delegation outcomes and sum each session's own cost."""
    failures: list[str] = []
    root_outcome = payload.get("outcome")
    final_text = payload.get("final_text")
    session_id = payload.get("session_id")
    if root_outcome not in SUCCESS_OUTCOMES:
        failures.append(f"root_outcome:{root_outcome}")
    if not isinstance(final_text, str) or not final_text.strip():
        failures.append("missing_final_text")
    if not isinstance(session_id, str) or not session_id:
        return {
            "complete": False,
            "failures": failures + ["missing_session_id"],
            "total_cost_usd": None,
            "guidelines_retrieved": 0,
            "retrieval_candidate_kinds": [],
        }

    traces_dir = state_root / ".fabri" / "traces"
    pending = [session_id]
    seen_sessions: set[str] = set()
    root_delegations: dict[str, str | None] = {}
    total_cost = 0.0
    saw_cost = False
    guidelines_retrieved = 0
    retrieval_kinds: set[str] = set()
    while pending:
        current_session = pending.pop(0)
        if current_session in seen_sessions:
            continue
        seen_sessions.add(current_session)
        trace_path = traces_dir / f"{current_session}.jsonl"
        events = _read_jsonl(trace_path)
        if not events:
            failures.append("missing_trace")
            continue
        failure_payloads: list[object] = []
        for event in events:
            event_type = event.get("type")
            if event_type == "cost_unaccounted":
                failures.append("cost_unaccounted")
                failure_payloads.append(event)
            elif event_type in {"failed", "error", "incomplete"}:
                failure_payloads.append(event)
            elif event_type == "usage":
                own_cost = event.get("cost_usd")
                if isinstance(own_cost, (int, float)):
                    total_cost += float(own_cost)
                    saw_cost = True
                retrieved = event.get("guidelines_retrieved")
                if isinstance(retrieved, int):
                    guidelines_retrieved += retrieved
            elif event_type == "post_run_usage":
                post_cost = event.get("cost_usd")
                if isinstance(post_cost, (int, float)):
                    total_cost += float(post_cost)
                    saw_cost = True
            elif event_type == "retrieval":
                candidates = event.get("candidates", [])
                if isinstance(candidates, list):
                    for candidate in candidates:
                        if isinstance(candidate, dict) and isinstance(candidate.get("kind"), str):
                            retrieval_kinds.add(candidate["kind"])
            if event_type != "tool_call":
                continue
            name = event.get("name")
            result = event.get("result")
            if not isinstance(result, dict):
                continue
            if result.get("ok") is not True:
                failure_payloads.append(result)
            child = result.get("result")
            if not isinstance(child, dict):
                continue
            child_session = child.get("session_id")
            child_outcome = child.get("outcome")
            if isinstance(child_session, str) and child_session:
                pending.append(child_session)
                if child_outcome not in SUCCESS_OUTCOMES:
                    failures.append(f"nested_outcome:{child_outcome}")
            if current_session == session_id and isinstance(name, str) and name in required_delegations:
                root_delegations[name] = (
                    child_outcome
                    if result.get("ok") is True and isinstance(child_outcome, str)
                    else None
                )
        # Successful child processes may emit best-effort narrator retry
        # warnings on stderr (the default narrator ceiling is 60 tokens). They
        # do not invalidate the artifact. Only inspect terminal/tool failures.
        failure_text = "\n".join(_failure_strings(failure_payloads)).casefold()
        if "max_tokens" in failure_text or "truncat" in failure_text:
            failures.append("truncation")
        if "timeout" in failure_text:
            failures.append("timeout")

    if not saw_cost:
        failures.append("missing_cost_usage")
    for required in required_delegations:
        if required not in root_delegations:
            failures.append(f"missing_required_delegation:{required}")
        elif root_delegations[required] not in SUCCESS_OUTCOMES:
            failures.append(
                f"failed_required_delegation:{required}:{root_delegations[required]}"
            )
    return {
        "complete": not failures,
        "failures": sorted(set(failures)),
        "total_cost_usd": round(total_cost, 6) if saw_cost else None,
        "guidelines_retrieved": guidelines_retrieved,
        "retrieval_candidate_kinds": sorted(retrieval_kinds),
    }


def _run_command(
    argv: list[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def build_source_manifest(
    case: ProbeCase,
    command_runner: CommandRunner,
    cwd: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str | bool | None]:
    """Return reproducibility metadata without requiring the roster to be a Git repo."""
    git_environment = dict(os.environ) if environment is None else environment
    source_parent = case.company_source.parent
    source_path = str(case.company_source)
    manifest_path = source_path
    company_source_sha256 = hashlib.sha256(case.company_source.read_bytes()).hexdigest()

    def without_git_metadata() -> dict[str, str | bool | None]:
        return {
            "path": source_path,
            "roster_revision": None,
            "roster_worktree_clean": None,
            "company_source_sha256": company_source_sha256,
        }

    try:
        top_level = command_runner(
            ["git", "-C", str(source_parent), "rev-parse", "--show-toplevel"],
            cwd,
            git_environment,
            MANIFEST_GIT_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return without_git_metadata()
    if top_level.returncode != 0:
        return without_git_metadata()

    try:
        manifest_path = str(case.company_source.relative_to(top_level.stdout.strip()))
    except ValueError:
        pass

    try:
        revision = command_runner(
            ["git", "-C", str(source_parent), "rev-parse", "HEAD"],
            cwd,
            git_environment,
            MANIFEST_GIT_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return without_git_metadata()
    if revision.returncode != 0:
        roster_revision: str | None = None
        roster_worktree_clean: bool | None = None
    else:
        try:
            worktree_status = command_runner(
                ["git", "-C", str(source_parent), "status", "--short"],
                cwd,
                git_environment,
                MANIFEST_GIT_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return without_git_metadata()
        if worktree_status.returncode != 0:
            roster_revision = None
            roster_worktree_clean = None
        else:
            roster_revision = revision.stdout.strip()
            roster_worktree_clean = not worktree_status.stdout.strip()

    return {
        "path": manifest_path,
        "roster_revision": roster_revision,
        "roster_worktree_clean": roster_worktree_clean,
        "company_source_sha256": company_source_sha256,
    }


def validate_publication_payload(payload: dict[str, object]) -> None:
    """Validate the stable public results schema before publishing it."""
    required_top_level: dict[str, tuple[type[object], ...]] = {
        "study": (str,),
        "generated_at": (str,),
        "case_id": (str,),
        "company": (str,),
        "fabri_version": (str, type(None)),
        "source": (dict,),
        "replicas_per_candidate": (int,),
        "selection_policy": (str,),
        "candidates": (list,),
        "recommendation": (str, type(None)),
        "status": (str,),
        "claim_boundary": (str,),
        "released_gate_cost_usd": (int, float, type(None)),
        "total_research_spend_usd": (int, float, type(None)),
    }
    for key, expected_types in required_top_level.items():
        if key not in payload:
            raise ProbeError(f"publication payload missing required key: {key}")
        value = payload[key]
        if not isinstance(value, expected_types) or (
            key
            in {
                "replicas_per_candidate",
                "released_gate_cost_usd",
                "total_research_spend_usd",
            }
            and isinstance(value, bool)
        ):
            raise ProbeError(f"publication payload field {key!r} has an invalid type")

    optional_top_level: dict[str, tuple[type[object], ...]] = {
        "judge_model": (str, type(None)),
        "judge_prompt_version": (str, type(None)),
        "judge_temperature": (int, float, type(None)),
    }
    for key, expected_types in optional_top_level.items():
        if key in payload and (
            not isinstance(payload[key], expected_types) or isinstance(payload[key], bool)
        ):
            raise ProbeError(f"publication payload field {key!r} has an invalid type")

    source = payload["source"]
    if not isinstance(source, dict):  # Kept for type narrowing after validation above.
        raise ProbeError("publication payload field 'source' has an invalid type")
    required_source: dict[str, tuple[type[object], ...]] = {
        "path": (str,),
        "roster_revision": (str, type(None)),
        "roster_worktree_clean": (bool, type(None)),
        "company_source_sha256": (str,),
    }
    for key, expected_types in required_source.items():
        if key not in source:
            raise ProbeError(f"publication payload source missing required key: {key}")
        if not isinstance(source[key], expected_types):
            raise ProbeError(f"publication payload source field {key!r} has an invalid type")

    candidates = payload["candidates"]
    if not isinstance(candidates, list):  # Kept for type narrowing after validation above.
        raise ProbeError("publication payload field 'candidates' has an invalid type")
    required_candidate: dict[str, tuple[type[object], ...]] = {
        "id": (str,),
        "overrides": (dict,),
        "configured_replicas": (int,),
        "scheduled_replicas": (int,),
        "preflights": (int,),
        "model_runs": (int,),
        "completion_rate": (int, float),
        "conditional_rubric_pass_rate": (int, float, type(None)),
        "end_to_end_pass_rate": (int, float),
        "median_total_cost_usd": (int, float, type(None)),
        "qualifies": (bool,),
        "runs": (list,),
    }
    optional_candidate: dict[str, tuple[type[object], ...]] = {
        "judge_pass_rate": (int, float, type(None)),
    }
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ProbeError(f"publication payload candidate {index} must be a mapping")
        for key, expected_types in required_candidate.items():
            if key not in candidate:
                raise ProbeError(
                    f"publication payload candidate {index} missing required key: {key}"
                )
            value = candidate[key]
            if not isinstance(value, expected_types) or (
                key
                in {
                    "configured_replicas",
                    "scheduled_replicas",
                    "preflights",
                    "model_runs",
                    "completion_rate",
                    "conditional_rubric_pass_rate",
                    "end_to_end_pass_rate",
                    "median_total_cost_usd",
                }
                and isinstance(value, bool)
            ):
                raise ProbeError(
                    f"publication payload candidate {index} field {key!r} has an invalid type"
                )
        for key, expected_types in optional_candidate.items():
            if key in candidate and (
                not isinstance(candidate[key], expected_types)
                or isinstance(candidate[key], bool)
            ):
                raise ProbeError(
                    f"publication payload candidate {index} field {key!r} has an invalid type"
                )
        if "decision" in candidate and not isinstance(
            candidate["decision"], (str, type(None))
        ):
            raise ProbeError(
                f"publication payload candidate {index} field 'decision' has an invalid type"
            )


def _write_private_attempt(
    attempt_root: Path,
    *,
    compile_process: subprocess.CompletedProcess[str],
    run_process: subprocess.CompletedProcess[str] | None,
    preflight: dict[str, object] | None,
    private_result: dict[str, object],
) -> None:
    private = attempt_root / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / "compile.stdout").write_text(compile_process.stdout, encoding="utf-8")
    (private / "compile.stderr").write_text(compile_process.stderr, encoding="utf-8")
    if run_process is not None:
        (private / "run.stdout").write_text(run_process.stdout, encoding="utf-8")
        (private / "run.stderr").write_text(run_process.stderr, encoding="utf-8")
    if preflight is not None:
        (private / "preflight.json").write_text(
            json.dumps(preflight, indent=2), encoding="utf-8"
        )
    (private / "result.json").write_text(
        json.dumps(private_result, indent=2), encoding="utf-8"
    )


def run_probe(
    case: ProbeCase,
    output_dir: str | Path,
    *,
    command_runner: CommandRunner = _run_command,
    judge: bool = False,
    judge_runner: JudgeRunner | None = None,
    run_timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
    cwd: Path | None = None,
) -> dict[str, object]:
    """Run isolated qualification replicas and write public aggregate artifacts."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work_root = output / "private-attempts"
    work_root.mkdir(parents=True, exist_ok=True)
    command_cwd = Path.cwd() if cwd is None else cwd
    environment = dict(os.environ)
    source_manifest = build_source_manifest(
        case,
        command_runner,
        command_cwd,
        environment=environment,
    )
    judge_config = (
        case.judge
        if case.judge is not None
        else (_default_judge_config() if judge else None)
    )
    active_judge_runner = judge_runner or _run_default_judge
    public_candidates: list[dict[str, object]] = []

    for candidate in case.candidates:
        public_runs: list[dict[str, object]] = []
        for replica in range(1, case.replicas + 1):
            attempt_root = work_root / candidate.candidate_id / f"replica-{replica:02d}"
            compiled_parent = attempt_root / "compiled"
            state_root = attempt_root / "state"
            attempt_root.mkdir(parents=True, exist_ok=False)
            compile_argv = [
                sys.executable,
                "-m",
                "fabri.cli",
                "company",
                "compile",
                str(case.company_source),
                "--dest",
                str(compiled_parent),
            ]
            compile_process = command_runner(
                compile_argv, command_cwd, environment, run_timeout_s
            )
            private_result: dict[str, object] = {
                "candidate": candidate.candidate_id,
                "replica": replica,
                "compile_returncode": compile_process.returncode,
            }
            if compile_process.returncode != 0:
                public_run = {
                    "replica": replica,
                    "attempt_status": "invalid_measurement",
                    "rubric_passed": None,
                    "failure_reasons": ["company_compile_failed"],
                    "total_cost_usd": None,
                }
                private_result.update(public_run)
                _write_private_attempt(
                    attempt_root,
                    compile_process=compile_process,
                    run_process=None,
                    preflight=None,
                    private_result=private_result,
                )
                public_runs.append(public_run)
                continue

            root_config = compiled_parent / case.company_name / f"{case.root_id}.yaml"
            try:
                changed_paths = apply_candidate(root_config, candidate)
                preflight = build_preflight_manifest(root_config)
            except (OSError, ProbeError, ValueError) as exc:
                public_run = {
                    "replica": replica,
                    "attempt_status": "invalid_measurement",
                    "rubric_passed": None,
                    "failure_reasons": ["preflight_failed"],
                    "total_cost_usd": None,
                }
                private_result.update(public_run)
                private_result["diagnostic"] = str(exc)
                _write_private_attempt(
                    attempt_root,
                    compile_process=compile_process,
                    run_process=None,
                    preflight=None,
                    private_result=private_result,
                )
                public_runs.append(public_run)
                continue

            if candidate.has_overrides() and not changed_paths:
                public_run = {
                    "replica": replica,
                    "attempt_status": "invalid_measurement",
                    "rubric_passed": None,
                    "failure_reasons": ["candidate_noop"],
                    "total_cost_usd": None,
                }
                private_result.update(public_run)
                private_result["changed_org_paths"] = changed_paths
                _write_private_attempt(
                    attempt_root,
                    compile_process=compile_process,
                    run_process=None,
                    preflight=preflight,
                    private_result=private_result,
                )
                public_runs.append(public_run)
                continue

            run_env = {**environment, "FABRI_HOME": str(state_root)}
            run_argv = [
                sys.executable,
                "-m",
                "fabri.cli",
                "--config",
                str(root_config),
                "run",
                case.holdout_prompt,
            ]
            started = time.monotonic()
            try:
                run_process = command_runner(run_argv, command_cwd, run_env, run_timeout_s)
                elapsed_s = round(time.monotonic() - started, 3)
            except subprocess.TimeoutExpired:
                public_run = {
                    "replica": replica,
                    "attempt_status": "operational_failure",
                    "rubric_passed": None,
                    "failure_reasons": ["root_process_timeout"],
                    "total_cost_usd": None,
                }
                private_result.update(public_run)
                private_result["changed_org_paths"] = changed_paths
                _write_private_attempt(
                    attempt_root,
                    compile_process=compile_process,
                    run_process=None,
                    preflight=preflight,
                    private_result=private_result,
                )
                public_runs.append(public_run)
                continue

            try:
                payload = _as_mapping(json.loads(run_process.stdout), "fabri run stdout")
                analysis = analyze_run(payload, state_root, case.required_delegations)
            except (json.JSONDecodeError, ProbeError) as exc:
                public_run = {
                    "replica": replica,
                    "attempt_status": "invalid_measurement",
                    "rubric_passed": None,
                    "failure_reasons": ["unreadable_run_result"],
                    "total_cost_usd": None,
                }
                private_result["diagnostic"] = str(exc)
            else:
                if run_process.returncode != 0:
                    cast_failures = analysis["failures"]
                    if isinstance(cast_failures, list):
                        cast_failures.append("root_process_nonzero")
                    analysis["complete"] = False
                if payload.get("success") is not True:
                    cast_failures = analysis["failures"]
                    if isinstance(cast_failures, list):
                        cast_failures.append("root_success_false")
                    analysis["complete"] = False
                complete = bool(analysis["complete"])
                final_text = payload.get("final_text")
                rubric = score_text(
                    final_text if isinstance(final_text, str) else "",
                    case.required_terms,
                    case.forbidden_terms,
                )
                structured = (
                    score_structured(payload.get("structured_output"), case.structured_fields)
                    if complete
                    else {"passed": None, "mismatches": []}
                )
                total_cost = analysis["total_cost_usd"]
                within_cost = (
                    case.company_max_cost_usd is None
                    or (isinstance(total_cost, (int, float)) and total_cost <= case.company_max_cost_usd)
                )
                failure_reasons = list(analysis["failures"])
                if complete and not within_cost:
                    failure_reasons.append("company_cost_limit_exceeded")
                judge_record: dict[str, object] | None = None
                if complete and judge_config is not None:
                    try:
                        judge_result = active_judge_runner(
                            judge_config,
                            final_text if isinstance(final_text, str) else "",
                        )
                        judge_record = {
                            "verdict": judge_result.verdict,
                            "rationale": judge_result.rationale,
                            "model": judge_config.model,
                            "prompt_version": JUDGE_PROMPT_VERSION,
                            "cost_usd": cost_for(judge_result.usage),
                        }
                    # Provider SDKs have no shared exception hierarchy. A judge
                    # failure remains advisory and cannot invalidate the run.
                    except Exception:
                        judge_record = {
                            "verdict": None,
                            "rationale": "judge_unavailable",
                            "model": judge_config.model,
                            "prompt_version": JUDGE_PROMPT_VERSION,
                            "cost_usd": None,
                        }
                public_run = {
                    "replica": replica,
                    "attempt_status": "complete" if complete else "operational_failure",
                    "rubric_passed": rubric["passed"] if complete else None,
                    "missing_required": rubric["missing"] if complete else [],
                    "forbidden_hits": rubric["forbidden"] if complete else [],
                    "structured_passed": structured["passed"],
                    "structured_mismatches": structured["mismatches"],
                    "within_cost_limit": within_cost if complete else None,
                    "end_to_end_passed": bool(
                        complete and rubric["passed"] and structured["passed"] and within_cost
                    ),
                    "failure_reasons": sorted(set(failure_reasons)),
                    "total_cost_usd": total_cost,
                    "judge_cost_usd": (
                        judge_record["cost_usd"] if judge_record is not None else None
                    ),
                    "judge": judge_record,
                    "wall_time_s": elapsed_s,
                    "guidelines_retrieved": analysis["guidelines_retrieved"],
                    "retrieval_candidate_kinds": analysis["retrieval_candidate_kinds"],
                }
                private_result.update({
                    "session_id": payload.get("session_id"),
                    "outcome": payload.get("outcome"),
                    "run_returncode": run_process.returncode,
                    "changed_org_paths": changed_paths,
                    **public_run,
                })
            if "changed_org_paths" not in private_result:
                private_result["changed_org_paths"] = changed_paths
                private_result.update(public_run)
            _write_private_attempt(
                attempt_root,
                compile_process=compile_process,
                run_process=run_process,
                preflight=preflight,
                private_result=private_result,
            )
            public_runs.append(public_run)

        for run in public_runs:
            run.setdefault("structured_passed", None)
            run.setdefault("structured_mismatches", [])
            run.setdefault("judge_cost_usd", None)
            run.setdefault("judge", None)

        completed = [run for run in public_runs if run["attempt_status"] == "complete"]
        passed = [run for run in public_runs if run.get("end_to_end_passed") is True]
        costs = [
            float(run["total_cost_usd"])
            for run in public_runs
            if isinstance(run.get("total_cost_usd"), (int, float))
        ]
        qualifies = len(passed) == case.replicas
        preflights = sum(
            run.get("failure_reasons") != ["company_compile_failed"]
            for run in public_runs
        )
        model_runs = sum(
            run.get("attempt_status") in {"complete", "operational_failure"}
            for run in public_runs
        )
        candidate_result: dict[str, object] = {
            "id": candidate.candidate_id,
            "overrides": candidate.overrides(),
            "configured_replicas": case.replicas,
            "scheduled_replicas": case.replicas,
            "preflights": preflights,
            "model_runs": model_runs,
            "completion_rate": len(completed) / case.replicas,
            "conditional_rubric_pass_rate": (
                sum(run.get("rubric_passed") is True for run in completed) / len(completed)
                if completed else None
            ),
            "judge_pass_rate": (
                sum(
                    isinstance(run.get("judge"), dict)
                    and run["judge"].get("verdict") == "pass"
                    for run in public_runs
                )
                / sum(isinstance(run.get("judge"), dict) for run in public_runs)
                if any(isinstance(run.get("judge"), dict) for run in public_runs)
                else None
            ),
            "end_to_end_pass_rate": len(passed) / case.replicas,
            "median_total_cost_usd": statistics.median(costs) if costs else None,
            "qualifies": qualifies,
            "runs": public_runs,
        }
        if model_runs == 0 and public_runs:
            first_failure_reasons = public_runs[0].get("failure_reasons")
            if (
                isinstance(first_failure_reasons, list)
                and len(first_failure_reasons) == 1
                and all(
                    run.get("failure_reasons") == first_failure_reasons
                    for run in public_runs
                )
            ):
                candidate_result["decision"] = first_failure_reasons[0]
        public_candidates.append(candidate_result)

    qualified = [candidate for candidate in public_candidates if candidate["qualifies"]]
    qualified.sort(
        key=lambda candidate: (
            candidate["median_total_cost_usd"] is None,
            candidate["median_total_cost_usd"] or 0.0,
        )
    )
    winner = qualified[0] if qualified else None

    def sum_costs(candidates: list[dict[str, object]]) -> float:
        return sum(
            float(run["total_cost_usd"])
            for candidate in candidates
            for run in candidate["runs"]
            if isinstance(run, dict)
            and isinstance(run.get("total_cost_usd"), (int, float))
        )

    try:
        fabri_version: str | None = importlib.metadata.version("fabri")
    except importlib.metadata.PackageNotFoundError:
        fabri_version = None
    payload = {
        "study": "company-setup-qualification",
        "generated_at": datetime.now(UTC).isoformat(),
        "case_id": case.case_id,
        "company": case.company_name,
        "fabri_version": fabri_version,
        "judge_model": judge_config.model if judge_config is not None else None,
        "judge_prompt_version": JUDGE_PROMPT_VERSION if judge_config is not None else None,
        "judge_temperature": JUDGE_TEMPERATURE if judge_config is not None else None,
        "source": source_manifest,
        "replicas_per_candidate": case.replicas,
        "selection_policy": (
            "qualify only at 100% scheduled end-to-end pass rate; among qualifiers "
            "choose the lowest median recursively-accounted total cost"
        ),
        "candidates": public_candidates,
        "recommendation": winner["id"] if winner else None,
        "status": "qualified" if winner else "no_viable_setup",
        "released_gate_cost_usd": sum_costs([winner]) if winner else None,
        # This is this invocation's spend only; the historical published value
        # also aggregated throwaway pilot runs by hand and is not reproduced here.
        "total_research_spend_usd": sum_costs(public_candidates),
        "claim_boundary": CLAIM_BOUNDARY,
    }
    validate_publication_payload(payload)
    (output / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output / "results.md").write_text(render_markdown(payload), encoding="utf-8")
    if winner is not None:
        profile = {
            "version": 1,
            "company": case.company_name,
            "workload": case.case_id,
            "overrides": winner["overrides"],
            "qualification": {
                "replicas": case.replicas,
                "end_to_end_pass_rate": winner["end_to_end_pass_rate"],
                "median_total_cost_usd": winner["median_total_cost_usd"],
            },
        }
        (output / "recommended-profile.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
        )
    return payload


def render_markdown(payload: dict[str, object]) -> str:
    source = payload.get("source", {})
    if not isinstance(source, dict):
        source = {}
    released_gate_cost = payload.get("released_gate_cost_usd")
    released_gate_cost_display = (
        "—"
        if released_gate_cost is None
        else f"${float(released_gate_cost):.6f}"
    )
    total_research_spend = payload.get("total_research_spend_usd")
    total_research_spend_display = (
        "—"
        if total_research_spend is None
        else f"${float(total_research_spend):.6f}"
    )
    lines = [
        "# Company setup qualification",
        "",
        f"- Case: `{payload['case_id']}`",
        f"- Company: `{payload['company']}`",
        f"- Status: **{payload['status']}**",
        f"- Recommendation: `{payload['recommendation'] or 'none'}`",
        f"- Fabri version: `{payload.get('fabri_version') or 'unavailable'}`",
        f"- Roster revision: `{source.get('roster_revision') or 'unavailable'}`",
        f"- Roster worktree clean: `{source.get('roster_worktree_clean')}`",
        f"- Company source SHA-256: `{source.get('company_source_sha256')}`",
        f"- Company source path: `{source.get('path')}`",
        f"- Released gate cost: {released_gate_cost_display}",
        f"- Total research spend: {total_research_spend_display}",
        f"- Claim boundary: {payload.get('claim_boundary')}",
        "",
        "| Candidate | Model runs | Decision | Completion | Conditional rubric | End-to-end | Median cost | Qualifies |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    candidates = payload.get("candidates", [])
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            conditional = candidate.get("conditional_rubric_pass_rate")
            cost = candidate.get("median_total_cost_usd")
            lines.append(
                f"| {candidate.get('id')} | {candidate.get('model_runs')} | "
                f"{candidate.get('decision', '—')} | "
                f"{float(candidate.get('completion_rate', 0)):.0%} | "
                f"{('—' if conditional is None else f'{float(conditional):.0%}')} | "
                f"{float(candidate.get('end_to_end_pass_rate', 0)):.0%} | "
                f"{('—' if cost is None else f'${float(cost):.4f}')} | "
                f"{'yes' if candidate.get('qualifies') else 'no'} |"
            )
    lines.extend([
        "",
        "Incomplete runs are operational failures and have no rubric verdict. "
        "Raw prompts, traces, session IDs, and model output remain under `private-attempts/`.",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--replicas", type=int, default=None)
    parser.add_argument("--run-timeout-s", type=float, default=DEFAULT_RUN_TIMEOUT_S)
    parser.add_argument(
        "--judge",
        action="store_true",
        help="run the optional frozen semantic judge (also enabled by a dataset judge block)",
    )
    args = parser.parse_args(argv)
    if args.run_timeout_s <= 0:
        parser.error("--run-timeout-s must be positive")
    try:
        case = load_probe_case(args.dataset, args.case_id, replicas_override=args.replicas)
        if args.judge:
            result = run_probe(
                case,
                args.output_dir,
                judge=True,
                run_timeout_s=args.run_timeout_s,
            )
        else:
            result = run_probe(case, args.output_dir, run_timeout_s=args.run_timeout_s)
    except (OSError, ProbeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "status": result["status"],
        "recommendation": result["recommendation"],
        "results": str((Path(args.output_dir).resolve() / "results.json")),
    }, indent=2))
    return 0 if result["status"] == "qualified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
