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
import statistics
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from fabri.config import load_config

SUCCESS_OUTCOMES = {"success", "success_with_recovery"}
MIN_TOKEN_FLOOR = 64
MAX_TOKEN_FLOOR = 4096
DEFAULT_RUN_TIMEOUT_S = 600.0
CLAIM_BOUNDARY = "setup qualification only; memory/control result pending"

CommandRunner = Callable[
    [list[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]


class ProbeError(ValueError):
    """Raised when a setup-probe input or compiled company is invalid."""


@dataclass(frozen=True)
class ProbeCandidate:
    candidate_id: str
    delegated_llm_max_tokens_floor: int | None = None


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


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProbeError(f"{field} must be a positive integer")
    return value


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
        floor = candidate.get("delegated_llm_max_tokens_floor")
        if floor is not None:
            if not isinstance(floor, int) or isinstance(floor, bool):
                raise ProbeError(f"candidate {candidate_name} token floor must be an integer")
            if not MIN_TOKEN_FLOOR <= floor <= MAX_TOKEN_FLOOR:
                raise ProbeError(
                    f"candidate {candidate_name} token floor must be between "
                    f"{MIN_TOKEN_FLOOR} and {MAX_TOKEN_FLOOR}"
                )
        unknown = set(candidate) - {"id", "delegated_llm_max_tokens_floor"}
        if unknown:
            raise ProbeError(
                f"candidate {candidate_name} contains unsupported settings: "
                + ", ".join(sorted(unknown))
            )
        candidates.append(ProbeCandidate(candidate_name, floor))

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
    def normalize(value: str) -> str:
        return " ".join(value.casefold().replace("-", " ").split())

    normalized = normalize(text)
    missing = [
        " | ".join(group)
        for group in required_terms
        if not any(normalize(phrase) in normalized for phrase in group)
    ]
    forbidden = [term for term in forbidden_terms if normalize(term) in normalized]
    return {"passed": not missing and not forbidden, "missing": missing, "forbidden": forbidden}


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
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
) -> dict[str, str | bool | None]:
    """Return reproducibility metadata without requiring the roster to be a Git repo."""
    git_environment = dict(os.environ) if environment is None else environment
    source_parent = case.company_source.parent
    source_path = str(case.company_source)

    top_level = command_runner(
        ["git", "-C", str(source_parent), "rev-parse", "--show-toplevel"],
        cwd,
        git_environment,
        timeout_s,
    )
    if top_level.returncode != 0:
        return {
            "path": source_path,
            "roster_revision": None,
            "roster_worktree_clean": None,
            "company_source_sha256": hashlib.sha256(
                case.company_source.read_bytes()
            ).hexdigest(),
        }

    try:
        source_path = str(case.company_source.relative_to(top_level.stdout.strip()))
    except ValueError:
        pass

    revision = command_runner(
        ["git", "-C", str(source_parent), "rev-parse", "HEAD"],
        cwd,
        git_environment,
        timeout_s,
    )
    if revision.returncode != 0:
        roster_revision: str | None = None
        roster_worktree_clean: bool | None = None
    else:
        worktree_status = command_runner(
            ["git", "-C", str(source_parent), "status", "--short"],
            cwd,
            git_environment,
            timeout_s,
        )
        if worktree_status.returncode != 0:
            roster_revision = None
            roster_worktree_clean = None
        else:
            roster_revision = revision.stdout.strip()
            roster_worktree_clean = not worktree_status.stdout.strip()

    return {
        "path": source_path,
        "roster_revision": roster_revision,
        "roster_worktree_clean": roster_worktree_clean,
        "company_source_sha256": hashlib.sha256(case.company_source.read_bytes()).hexdigest(),
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
        timeout_s=run_timeout_s,
    )
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
                changed_paths = apply_delegated_token_floor(
                    root_config, candidate.delegated_llm_max_tokens_floor
                )
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

            if candidate.delegated_llm_max_tokens_floor is not None and not changed_paths:
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
                total_cost = analysis["total_cost_usd"]
                within_cost = (
                    case.company_max_cost_usd is None
                    or (isinstance(total_cost, (int, float)) and total_cost <= case.company_max_cost_usd)
                )
                failure_reasons = list(analysis["failures"])
                if complete and not within_cost:
                    failure_reasons.append("company_cost_limit_exceeded")
                public_run = {
                    "replica": replica,
                    "attempt_status": "complete" if complete else "operational_failure",
                    "rubric_passed": rubric["passed"] if complete else None,
                    "missing_required": rubric["missing"] if complete else [],
                    "forbidden_hits": rubric["forbidden"] if complete else [],
                    "within_cost_limit": within_cost if complete else None,
                    "end_to_end_passed": bool(complete and rubric["passed"] and within_cost),
                    "failure_reasons": sorted(set(failure_reasons)),
                    "total_cost_usd": total_cost,
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
            "overrides": {
                "delegated_llm_max_tokens_floor": candidate.delegated_llm_max_tokens_floor,
            },
            "configured_replicas": case.replicas,
            "scheduled_replicas": case.replicas,
            "preflights": preflights,
            "model_runs": model_runs,
            "completion_rate": len(completed) / case.replicas,
            "conditional_rubric_pass_rate": (
                sum(run.get("rubric_passed") is True for run in completed) / len(completed)
                if completed else None
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
    args = parser.parse_args(argv)
    if args.run_timeout_s <= 0:
        parser.error("--run-timeout-s must be positive")
    try:
        case = load_probe_case(args.dataset, args.case_id, replicas_override=args.replicas)
        result = run_probe(
            case,
            args.output_dir,
            run_timeout_s=args.run_timeout_s,
        )
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
