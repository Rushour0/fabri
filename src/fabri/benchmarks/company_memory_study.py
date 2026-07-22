"""Run a subprocess-isolated company memory-versus-control study.

Usage:
    python -m fabri.benchmarks.company_memory_study \
        --dataset benchmarks/datasets/company_memory_experiments.yaml \
        --case support_hq_safe_incident_response \
        --output-dir benchmarks/runs/support-hq-memory-study
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from fabri.benchmarks.company_record import score_arm_output
from fabri.benchmarks.company_setup_probe import (
    DEFAULT_RUN_TIMEOUT_S,
    SUCCESS_OUTCOMES,
    CommandRunner,
    ProbeError,
    _as_required_groups,
    _as_string_list,
    _run_command,
    analyze_run,
    build_source_manifest,
)


CLAIM_BOUNDARY = (
    "This measures this roster company's related-task performance when its learned "
    "SQLite memory is copied into a fresh holdout compile; it does not establish "
    "general memory effectiveness."
)
_ALLOWED_CONDITIONS = {"memory", "control"}
_ALLOWED_RETRIEVAL_STRATEGIES = {"dense", "sparse", "hybrid", "hybrid+mmr"}
_STRUCTURED_RESPONSE_INSTRUCTION = (
    "For the final executive response, return the JSON object matching "
    "agent.response_schema first. Put all user-facing prose in the response field. "
    "After the complete JSON object, append the required AGENT_MEMORY block."
)


@dataclass(frozen=True)
class MemoryCase:
    """The declarative inputs needed for one isolated memory study."""

    case_id: str
    company_source: Path
    company_name: str
    namespace: str
    root_id: str
    training_prompt: str
    holdout_prompt: str
    required_terms: tuple[tuple[str, ...], ...]
    forbidden_terms: tuple[str, ...]
    required_delegations: tuple[str, ...]
    retrieval_expectations: dict[str, object]
    replicas: int
    conditions: tuple[str, ...]
    structured_fields: Mapping[str, object] = field(default_factory=dict)
    response_schema: Mapping[str, object] = field(default_factory=dict)
    legacy_required_terms: tuple[tuple[str, ...], ...] | None = None
    legacy_forbidden_terms: tuple[str, ...] | None = None


@dataclass(frozen=True)
class MemoryDbSpec:
    """One unique SQLite database declared by compiled agent configs."""

    relative_path: Path
    agent_ids: tuple[str, ...]
    config_paths: tuple[str, ...]


def _as_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbeError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProbeError(f"{field} must be a positive integer")
    return value


def _validate_retrieval_overrides(
    top_k: int | None, strategy: str | None
) -> None:
    """Validate optional retrieval settings accepted by the study runner."""
    if top_k is not None and (
        not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 50
    ):
        raise ProbeError("--retrieval-top-k must be an integer between 1 and 50")
    if strategy is not None and strategy not in _ALLOWED_RETRIEVAL_STRATEGIES:
        choices = ", ".join(sorted(_ALLOWED_RETRIEVAL_STRATEGIES))
        raise ProbeError(f"--retrieval-strategy must be one of: {choices}")


def _load_dataset(path: Path) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProbeError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ProbeError(f"malformed YAML in {path}: {exc}") from exc
    return _as_mapping(loaded, str(path))


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{field} must be a non-empty string")
    return value


def _conditions(value: object, field: str) -> tuple[str, ...]:
    conditions = _as_string_list(value, field)
    if not conditions:
        raise ProbeError(f"{field} must not be empty")
    if len(set(conditions)) != len(conditions):
        raise ProbeError(f"{field} must not contain duplicates")
    unsupported = set(conditions) - _ALLOWED_CONDITIONS
    if unsupported:
        raise ProbeError(
            f"{field} contains unsupported conditions: {', '.join(sorted(unsupported))}"
        )
    return conditions


def _validated_response_schema(
    value: object,
    structured_fields: Mapping[str, object],
    field: str,
) -> dict[str, object]:
    """Accept only the value-free string schema used by memory holdouts."""
    schema = _as_mapping(value, field)
    allowed_top_level = {"type", "required", "properties"}
    if set(schema) != allowed_top_level or schema.get("type") != "object":
        raise ProbeError(
            f"{field} must contain exactly type, required, and properties for an object"
        )
    properties = _as_mapping(schema.get("properties"), f"{field}.properties")
    expected_keys = {"response", *structured_fields}
    if set(properties) != expected_keys:
        raise ProbeError(
            f"{field}.properties must be response plus every expected structured field"
        )
    for key, property_schema in properties.items():
        if property_schema != {"type": "string"}:
            raise ProbeError(
                f"{field}.properties.{key} must expose only type: string"
            )
    required = _as_string_list(schema.get("required"), f"{field}.required")
    if len(required) != len(set(required)) or set(required) != expected_keys:
        raise ProbeError(f"{field}.required must contain every schema property exactly once")
    return schema


def load_memory_case(
    dataset_path: str | Path,
    case_id: str,
    *,
    replicas_override: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> MemoryCase:
    """Load a company-memory dataset case and resolve its roster source."""
    dataset = _load_dataset(Path(dataset_path))
    defaults = _as_mapping(dataset.get("defaults"), "defaults")
    roster_root_env = _required_string(defaults.get("roster_root_env"), "defaults.roster_root_env")
    environment = os.environ if environ is None else environ
    roster_root = environment.get(roster_root_env)
    if not roster_root:
        raise ProbeError(f"{roster_root_env} is not set")

    cases = dataset.get("cases")
    if not isinstance(cases, list):
        raise ProbeError("cases must be a list")
    selected: dict[str, object] | None = None
    for index, raw_case in enumerate(cases):
        candidate = _as_mapping(raw_case, f"cases[{index}]")
        if candidate.get("id") == case_id:
            selected = candidate
            break
    if selected is None:
        raise ProbeError(f"case not found: {case_id}")

    source_value = _required_string(
        selected.get("company_source"), f"case {case_id}.company_source"
    )
    company_source = (Path(roster_root) / source_value).resolve()
    if not company_source.is_file():
        raise ProbeError(f"company source not found for {case_id}: {company_source}")
    try:
        company_data = tomllib.loads(company_source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProbeError(f"could not read company source {company_source}: {exc}") from exc
    company = _as_mapping(company_data.get("company"), "company")
    company_name = _required_string(company.get("name"), "company.name")
    namespace = _required_string(company.get("memory_namespace"), "company.memory_namespace")
    nodes = company_data.get("node")
    if not isinstance(nodes, list):
        raise ProbeError("company nodes must be a list")
    root_ids = [
        node.get("id")
        for node in nodes
        if isinstance(node, dict) and node.get("report_to") == ""
    ]
    if len(root_ids) != 1 or not isinstance(root_ids[0], str) or not root_ids[0]:
        raise ProbeError("company must contain exactly one root node")

    training_prompt = _required_string(
        selected.get("training_prompt"), f"case {case_id}.training_prompt"
    )
    holdout_prompt = _required_string(
        selected.get("holdout_prompt"), f"case {case_id}.holdout_prompt"
    )
    expected = _as_mapping(selected.get("expected"), f"case {case_id}.expected")
    legacy_expected_value = selected.get("legacy_expected")
    legacy_expected = (
        None
        if legacy_expected_value is None
        else _as_mapping(
            legacy_expected_value,
            f"case {case_id}.legacy_expected",
        )
    )
    structured_fields = _as_mapping(
        expected.get("structured"), f"case {case_id}.expected.structured"
    )
    if not structured_fields:
        raise ProbeError(f"case {case_id}.expected.structured must not be empty")
    response_schema = _validated_response_schema(
        selected.get("response_schema"),
        structured_fields,
        f"case {case_id}.response_schema",
    )
    setup = _as_mapping(selected.get("setup_probe"), f"case {case_id}.setup_probe")
    retrieval_expectations = _as_mapping(
        selected.get("retrieval_expectations"), f"case {case_id}.retrieval_expectations"
    )
    replicas_value = selected.get("replicas", defaults.get("replicas"))
    replicas = (
        _positive_int(replicas_override, "--replicas")
        if replicas_override is not None
        else _positive_int(replicas_value, f"case {case_id}.replicas")
    )
    conditions = _conditions(
        selected.get("conditions", defaults.get("conditions", ["memory", "control"])),
        f"case {case_id}.conditions",
    )
    return MemoryCase(
        case_id=case_id,
        company_source=company_source,
        company_name=company_name,
        namespace=namespace,
        root_id=root_ids[0],
        training_prompt=training_prompt,
        holdout_prompt=holdout_prompt,
        required_terms=_as_required_groups(expected.get("required"), "expected.required"),
        structured_fields=structured_fields,
        forbidden_terms=_as_string_list(expected.get("forbidden"), "expected.forbidden"),
        response_schema=response_schema,
        legacy_required_terms=(
            _as_required_groups(
                legacy_expected.get("required"),
                f"case {case_id}.legacy_expected.required",
            )
            if legacy_expected is not None
            else None
        ),
        legacy_forbidden_terms=(
            _as_string_list(
                legacy_expected.get("forbidden"),
                f"case {case_id}.legacy_expected.forbidden",
            )
            if legacy_expected is not None
            else None
        ),
        required_delegations=_as_string_list(
            setup.get("required_delegations"),
            f"case {case_id}.setup_probe.required_delegations",
        ),
        retrieval_expectations=retrieval_expectations,
        replicas=replicas,
        conditions=conditions,
    )


def apply_holdout_response_contract(
    root_config: Path,
    response_schema: Mapping[str, object],
) -> None:
    """Inject strict structured output into one freshly compiled holdout root."""
    try:
        loaded = yaml.safe_load(root_config.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProbeError(f"could not read compiled root config {root_config}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProbeError(f"malformed YAML in compiled root config {root_config}: {exc}") from exc
    raw = _as_mapping(loaded, str(root_config))
    agent = _as_mapping(raw.get("agent"), f"{root_config}.agent")
    system_prompt = _required_string(
        agent.get("system_prompt"), f"{root_config}.agent.system_prompt"
    )
    if _STRUCTURED_RESPONSE_INSTRUCTION not in system_prompt:
        agent["system_prompt"] = (
            system_prompt.rstrip() + "\n\n" + _STRUCTURED_RESPONSE_INSTRUCTION
        )
    agent["response_schema"] = dict(response_schema)
    agent["response_retries"] = 1
    agent["error_strategy"] = "strict"
    raw["agent"] = agent
    try:
        root_config.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ProbeError(f"could not write compiled root config {root_config}: {exc}") from exc


def _compile_company(
    case: MemoryCase,
    destination: Path,
    command_runner: CommandRunner,
    command_cwd: Path,
    environment: Mapping[str, str],
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    return command_runner(
        [
            sys.executable,
            "-m",
            "fabri.cli",
            "company",
            "compile",
            str(case.company_source),
            "--dest",
            str(destination),
        ],
        command_cwd,
        environment,
        timeout_s,
    )


def apply_retrieval_overrides(
    compiled_dest: Path,
    company_name: str,
    *,
    top_k: int | None,
    strategy: str | None,
    verification: str | None = None,
    mining_enabled: bool | None = None,
    retrieval_enabled: bool | None = None,
) -> list[str]:
    """Rewrite optional retrieval settings into every raw compiled node config."""
    _validate_retrieval_overrides(top_k, strategy)
    if verification is not None and verification not in {"any", "verified"}:
        raise ProbeError("memory retrieval verification must be 'any' or 'verified'")
    if (
        top_k is None
        and strategy is None
        and verification is None
        and mining_enabled is None
        and retrieval_enabled is None
    ):
        return []

    changed: list[str] = []
    for config_path in sorted((compiled_dest / company_name).rglob("*.yaml")):
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProbeError(f"could not read compiled config {config_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ProbeError(f"malformed YAML in compiled config {config_path}: {exc}") from exc
        raw = _as_mapping(loaded, str(config_path))
        memory = _as_mapping(raw.setdefault("memory", {}), f"{config_path}.memory")
        raw["memory"] = memory
        if top_k is not None:
            memory["top_k"] = top_k
        if strategy is not None:
            memory["retrieval_strategy"] = strategy
        if verification is not None:
            memory["retrieval_verification"] = verification
        if mining_enabled is not None:
            memory["mining_enabled"] = mining_enabled
        if retrieval_enabled is not None:
            memory["retrieval_enabled"] = retrieval_enabled
        try:
            config_path.write_text(
                yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
        except OSError as exc:
            raise ProbeError(f"could not write compiled config {config_path}: {exc}") from exc
        changed.append(str(config_path))
    return changed


def discover_memory_dbs(
    compiled_destination: Path, company_name: str
) -> tuple[MemoryDbSpec, ...]:
    """Return every unique, company-local SQLite path declared by compiled YAML."""
    company_root = (compiled_destination / company_name).resolve()
    declarations: dict[Path, dict[str, set[str]]] = {}
    for config_path in sorted(company_root.rglob("*.yaml")):
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProbeError(f"could not read compiled config {config_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ProbeError(f"malformed YAML in compiled config {config_path}: {exc}") from exc
        raw = _as_mapping(loaded, str(config_path))
        memory = raw.get("memory")
        if not isinstance(memory, dict) or memory.get("backend") not in (None, "sqlite"):
            continue
        sqlite_path = memory.get("sqlite_path")
        if not isinstance(sqlite_path, str) or not sqlite_path:
            continue
        declared = Path(sqlite_path)
        resolved = (
            declared.resolve()
            if declared.is_absolute()
            else (company_root / declared).resolve()
        )
        if not resolved.is_relative_to(company_root):
            raise ProbeError(
                f"compiled memory path escapes company root: {config_path}: {sqlite_path}"
            )
        relative = resolved.relative_to(company_root)
        agent = raw.get("agent")
        agent_id = (
            str(agent.get("name"))
            if isinstance(agent, dict) and agent.get("name")
            else config_path.stem
        )
        item = declarations.setdefault(relative, {"agents": set(), "configs": set()})
        item["agents"].add(agent_id)
        item["configs"].add(str(config_path.relative_to(company_root)))
    if not declarations:
        raise ProbeError(f"compiled company declares no SQLite memory DBs: {company_root}")
    return tuple(
        MemoryDbSpec(
            relative_path=relative,
            agent_ids=tuple(sorted(values["agents"])),
            config_paths=tuple(sorted(values["configs"])),
        )
        for relative, values in sorted(declarations.items(), key=lambda item: str(item[0]))
    )


def _db_manifest(path: Path, spec: MemoryDbSpec) -> dict[str, object]:
    """Build a transport-safe manifest without loading the embedding extension."""
    payload: dict[str, object] = {
        "path": str(spec.relative_path),
        "agent_ids": list(spec.agent_ids),
        "config_paths": list(spec.config_paths),
        "present": path.is_file(),
        "size_bytes": None,
        "sha256": None,
        "entry_ids": [],
        "entry_payload_sha256": None,
    }
    if not path.is_file():
        return payload
    data = path.read_bytes()
    payload["size_bytes"] = len(data)
    payload["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT id, payload FROM guidelines ORDER BY id"
            ).fetchall()
    except sqlite3.DatabaseError:
        return payload
    payload["entry_ids"] = [str(row[0]) for row in rows]
    canonical = json.dumps(
        [(str(entry_id), str(entry_payload)) for entry_id, entry_payload in rows],
        separators=(",", ":"),
    ).encode("utf-8")
    payload["entry_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _manifest_for_specs(
    compiled_destination: Path, company_name: str, specs: tuple[MemoryDbSpec, ...]
) -> list[dict[str, object]]:
    company_root = compiled_destination / company_name
    return [_db_manifest(company_root / spec.relative_path, spec) for spec in specs]


def _trace_events(state_root: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for trace in sorted((state_root / ".fabri" / "traces").glob("*.jsonl")):
        for line in trace.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _funnel_observations(state_root: Path) -> dict[str, object]:
    events = _trace_events(state_root)
    reports = [event for event in events if event.get("type") == "mining_report"]
    retrieval_ids = {
        str(candidate["id"])
        for event in events
        if event.get("type") == "retrieval"
        for candidate in event.get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("id")
    }
    repair_retries = sum(event.get("type") == "repair_attempt" for event in events)
    structured_retries = sum(
        event.get("type") == "structured_output"
        and isinstance(event.get("attempt"), int)
        and int(event["attempt"]) > 0
        for event in events
    )
    provider_retries = sum(
        int(event.get("provider_transient_retries", 0) or 0)
        for event in events
        if event.get("type") in {"usage", "post_run_usage"}
    )
    max_token_retries = sum(
        int(event.get("max_token_retries", 0) or 0)
        for event in events
        if event.get("type") in {"usage", "post_run_usage"}
    )
    return {
        "mining_reports": reports,
        "retrieved_entry_ids": sorted(retrieval_ids),
        "repair_retries": repair_retries,
        "structured_output_retries": structured_retries,
        "provider_transient_retries": provider_retries,
        "max_token_retries": max_token_retries,
        "total_retries": (
            repair_retries + structured_retries + provider_retries + max_token_retries
        ),
        "cost_unaccounted": any(event.get("type") == "cost_unaccounted" for event in events),
    }


def _run_company(
    root_config: Path,
    prompt: str,
    state_root: Path,
    command_runner: CommandRunner,
    command_cwd: Path,
    environment: Mapping[str, str],
    timeout_s: float,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    process = command_runner(
        [sys.executable, "-m", "fabri.cli", "--config", str(root_config), "run", prompt],
        command_cwd,
        {**environment, "FABRI_HOME": str(state_root)},
        timeout_s,
    )
    return process, round(time.monotonic() - started, 3)


def _parse_payload(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        loaded = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"malformed fabri run stdout: {exc}") from exc
    return _as_mapping(loaded, "fabri run stdout")


def _analysis_for_process(
    process: subprocess.CompletedProcess[str],
    payload: dict[str, object],
    state_root: Path,
    required_delegations: tuple[str, ...],
) -> dict[str, object]:
    analysis = analyze_run(payload, state_root, required_delegations)
    failures = analysis["failures"]
    if not isinstance(failures, list):
        raise ProbeError("analyze_run returned invalid failures")
    if process.returncode != 0:
        failures.append("root_process_nonzero")
        analysis["complete"] = False
    if payload.get("success") is not True:
        failures.append("root_success_false")
        analysis["complete"] = False
    analysis["failures"] = sorted(set(str(item) for item in failures))
    return analysis


def _private_write(
    attempt_root: Path,
    processes: Mapping[str, subprocess.CompletedProcess[str] | None],
    result: Mapping[str, object],
) -> None:
    private = attempt_root / "private"
    private.mkdir(parents=True, exist_ok=True)
    for label, process in processes.items():
        if process is None:
            continue
        (private / f"{label}.stdout").write_text(process.stdout, encoding="utf-8")
        (private / f"{label}.stderr").write_text(process.stderr, encoding="utf-8")
    private_result = dict(result)
    private_result.setdefault("structured_output", None)
    (private / "result.json").write_text(
        json.dumps(private_result, indent=2), encoding="utf-8"
    )


# Reasons that mean the training phase itself never produced a successful,
# complete run.  Every other invalid-run reason is only reachable after training
# already validated, so it belongs to the holdout/transport phase and must not
# mark training as failed.
_TRAINING_PHASE_REASONS = frozenset(
    {
        "training_compile_timeout",
        "training_compile_failed",
        "training_memory_manifest_invalid",
        "training_root_config_missing",
        "training_run_timeout",
        "training_result_unreadable",
        "training_failed",
    }
)


def _invalid_run(
    replica: int,
    condition: str,
    training_outcome: object,
    training_cost: object,
    reason: str,
    *,
    training_failures: list[str] | None = None,
    execution_order: int = 0,
) -> dict[str, object]:
    # ``training_success`` is authoritative here, not the agent's self-report: a
    # run that failed in the training phase never counts as a training success,
    # even when the raw payload optimistically reported ``success: true`` (a
    # truncated run does exactly this).  Holdout and transport reasons are only
    # reachable after training already validated, so training stays successful
    # and the reason is recorded against the holdout phase instead of leaking
    # into ``training_failure_reasons``.
    training_phase = reason in _TRAINING_PHASE_REASONS
    if training_phase:
        training_success = False
        training_failure_reasons = sorted(set((training_failures or []) + [reason]))
        holdout_failure_reasons: list[str] = []
    else:
        training_success = True
        training_failure_reasons = sorted(set(training_failures or []))
        holdout_failure_reasons = [reason]
    return {
        "replica": replica,
        "condition": condition,
        "holdout_complete": False,
        "rubric_passed": None,
        "scoring_mode": "structured",
        "missing_required": [],
        "forbidden_hits": [],
        "total_cost_usd": training_cost if isinstance(training_cost, (int, float)) else None,
        "guidelines_retrieved": 0,
        "retrieval_candidate_kinds": [],
        "training_outcome": training_outcome if isinstance(training_outcome, str) else None,
        "training_success": training_success,
        "training_failure_reasons": training_failure_reasons,
        "holdout_failure_reasons": holdout_failure_reasons,
        "execution_order": execution_order,
        "funnel": {
            "supply": {"mining_reports": [], "dbs": []},
            "transport": {"dbs": [], "intact": False},
            "retrieval": {"entry_ids": [], "guidelines_retrieved": 0},
            "outcome": {
                "rubric_passed": None,
                "repair_retries": 0,
                "structured_output_retries": 0,
                "provider_transient_retries": 0,
                "max_token_retries": 0,
                "total_retries": 0,
            },
        },
    }


def _sum_costs(*values: object) -> float | None:
    costs = [float(value) for value in values if isinstance(value, (int, float))]
    return round(sum(costs), 6) if costs else None


def _run_pair(
    case: MemoryCase,
    replica: int,
    condition: str,
    work_root: Path,
    command_runner: CommandRunner,
    command_cwd: Path,
    environment: Mapping[str, str],
    timeout_s: float,
    retrieval_top_k: int | None,
    retrieval_strategy: str | None,
    execution_order: int,
) -> dict[str, object]:
    """Run one training-to-fresh-holdout pair, retaining raw material privately."""
    attempt_root = work_root / f"replica-{replica:02d}" / condition
    attempt_root.mkdir(parents=True, exist_ok=False)
    train_dest = attempt_root / "training-compiled"
    train_state = attempt_root / "training-state"
    holdout_dest = attempt_root / "holdout-compiled"
    holdout_state = attempt_root / "holdout-state"
    processes: dict[str, subprocess.CompletedProcess[str] | None] = {
        "training-compile": None,
        "training-run": None,
        "holdout-compile": None,
        "holdout-run": None,
    }

    try:
        train_compile = _compile_company(
            case, train_dest, command_runner, command_cwd, environment, timeout_s
        )
    except subprocess.TimeoutExpired:
        result = _invalid_run(
            replica, condition, None, None, "training_compile_timeout",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    processes["training-compile"] = train_compile
    if train_compile.returncode != 0:
        result = _invalid_run(
            replica, condition, None, None, "training_compile_failed",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    apply_retrieval_overrides(
        train_dest,
        case.company_name,
        top_k=retrieval_top_k,
        strategy=retrieval_strategy,
    )
    try:
        train_specs = discover_memory_dbs(train_dest, case.company_name)
    except ProbeError as exc:
        result = _invalid_run(
            replica, condition, None, None, "training_memory_manifest_invalid",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, {**result, "diagnostic": str(exc)})
        return result

    train_root = train_dest / case.company_name / f"{case.root_id}.yaml"
    if not train_root.is_file():
        result = _invalid_run(
            replica, condition, None, None, "training_root_config_missing",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    try:
        train_run, train_elapsed = _run_company(
            train_root,
            case.training_prompt,
            train_state,
            command_runner,
            command_cwd,
            environment,
            timeout_s,
        )
        processes["training-run"] = train_run
        train_payload = _parse_payload(train_run)
        train_analysis = _analysis_for_process(train_run, train_payload, train_state, ())
    except subprocess.TimeoutExpired:
        result = _invalid_run(
            replica, condition, None, None, "training_run_timeout",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    except ProbeError as exc:
        result = _invalid_run(
            replica, condition, None, None, "training_result_unreadable",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, {**result, "diagnostic": str(exc)})
        return result

    training_outcome = train_payload.get("outcome")
    training_success = (
        train_payload["success"] if isinstance(train_payload.get("success"), bool) else None
    )
    training_cost = train_analysis.get("total_cost_usd")
    training_failures = list(train_analysis.get("failures", []))
    if training_outcome not in SUCCESS_OUTCOMES or not bool(train_analysis.get("complete")):
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "training_failed",
            training_failures=training_failures,            execution_order=execution_order,
        )
        _private_write(
            attempt_root,
            processes,
            {
                **result,
                "training_wall_time_s": train_elapsed,
                "training_outcome": training_outcome,
            },
        )
        return result

    try:
        holdout_compile = _compile_company(
            case, holdout_dest, command_runner, command_cwd, environment, timeout_s
        )
    except subprocess.TimeoutExpired:
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "holdout_compile_timeout",            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    processes["holdout-compile"] = holdout_compile
    if holdout_compile.returncode != 0:
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "holdout_compile_failed",            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    apply_retrieval_overrides(
        holdout_dest,
        case.company_name,
        top_k=retrieval_top_k,
        strategy=retrieval_strategy,
        mining_enabled=False if condition == "control" else None,
        retrieval_enabled=False if condition == "control" else None,
    )

    try:
        holdout_specs = discover_memory_dbs(holdout_dest, case.company_name)
    except ProbeError as exc:
        result = _invalid_run(
            replica, condition, training_outcome, training_cost,
            "holdout_memory_manifest_invalid",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, {**result, "diagnostic": str(exc)})
        return result

    train_by_path = {spec.relative_path: spec for spec in train_specs}
    holdout_by_path = {spec.relative_path: spec for spec in holdout_specs}
    if set(train_by_path) != set(holdout_by_path):
        result = _invalid_run(
            replica, condition, training_outcome, training_cost,
            "memory_manifest_mismatch",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result

    training_db_manifest = _manifest_for_specs(train_dest, case.company_name, train_specs)
    training_funnel = _funnel_observations(train_state)

    holdout_root = holdout_dest / case.company_name / f"{case.root_id}.yaml"
    if condition == "memory":
        missing = [item["path"] for item in training_db_manifest if item["present"] is not True]
        if missing:
            result = _invalid_run(
                replica,
                condition,
                training_outcome,
                training_cost,
                "training_memory_db_missing",                execution_order=execution_order,
            )
            _private_write(attempt_root, processes, {**result, "missing_memory_dbs": missing})
            return result
        for relative in train_by_path:
            source = train_dest / case.company_name / relative
            target = holdout_dest / case.company_name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    else:
        # A real fresh compile has no learned DB.  Removing a pre-created empty
        # sqlite file also keeps injected offline fakes from accidentally acting
        # as a control-memory source.
        for relative in holdout_by_path:
            (holdout_dest / case.company_name / relative).unlink(missing_ok=True)

    transported_db_manifest = _manifest_for_specs(
        holdout_dest, case.company_name, holdout_specs
    )
    transport_intact = condition == "memory" and all(
        before["sha256"] == after["sha256"]
        and before["entry_payload_sha256"] == after["entry_payload_sha256"]
        and before["entry_ids"] == after["entry_ids"]
        and before["present"] is True
        and after["present"] is True
        for before, after in zip(training_db_manifest, transported_db_manifest, strict=True)
    )
    if condition == "control":
        transport_intact = all(item["present"] is False for item in transported_db_manifest)
    if not holdout_root.is_file():
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "holdout_root_config_missing",            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    try:
        apply_holdout_response_contract(holdout_root, case.response_schema)
    except ProbeError as exc:
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "holdout_response_contract_invalid",
            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, {**result, "diagnostic": str(exc)})
        return result

    try:
        holdout_run, holdout_elapsed = _run_company(
            holdout_root,
            case.holdout_prompt,
            holdout_state,
            command_runner,
            command_cwd,
            environment,
            timeout_s,
        )
        processes["holdout-run"] = holdout_run
        holdout_payload = _parse_payload(holdout_run)
        holdout_analysis = _analysis_for_process(
            holdout_run, holdout_payload, holdout_state, case.required_delegations
        )
    except subprocess.TimeoutExpired:
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "holdout_run_timeout",            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, result)
        return result
    except ProbeError as exc:
        result = _invalid_run(
            replica,
            condition,
            training_outcome,
            training_cost,
            "holdout_result_unreadable",            execution_order=execution_order,
        )
        _private_write(attempt_root, processes, {**result, "diagnostic": str(exc)})
        return result

    guidelines_retrieved = holdout_analysis.get("guidelines_retrieved")
    if not isinstance(guidelines_retrieved, int):
        raise ProbeError("analyze_run returned invalid guidelines_retrieved")
    holdout_failures = list(holdout_analysis.get("failures", []))
    complete = bool(holdout_analysis.get("complete"))
    if condition == "control" and guidelines_retrieved != 0:
        complete = False
        holdout_failures.append("control_guidelines_retrieved_nonzero")
    final_text = holdout_payload.get("final_text")
    structured_output = holdout_payload.get("structured_output")
    rubric = score_arm_output(
        {"structured_output": structured_output},
        final_text if isinstance(final_text, str) else None,
        case.required_terms,
        case.structured_fields,
        case.forbidden_terms,
    )
    if rubric is None:  # Structured mode always returns a deterministic verdict.
        raise ProbeError("structured scoring unexpectedly returned no verdict")
    holdout_funnel = _funnel_observations(holdout_state)
    transported_entry_ids = {
        str(entry_id)
        for item in transported_db_manifest
        for entry_id in item.get("entry_ids", [])
    }
    retrieved_entry_ids = set(holdout_funnel["retrieved_entry_ids"])
    result = {
        "replica": replica,
        "condition": condition,
        "holdout_complete": complete,
        "rubric_passed": rubric["passed"] if complete else None,
        "scoring_mode": rubric["scoring_mode"],
        "missing_required": rubric["missing"] if complete else [],
        "forbidden_hits": rubric["forbidden"] if complete else [],
        "total_cost_usd": _sum_costs(training_cost, holdout_analysis.get("total_cost_usd")),
        "guidelines_retrieved": guidelines_retrieved,
        "retrieval_candidate_kinds": holdout_analysis.get("retrieval_candidate_kinds", []),
        "training_outcome": training_outcome if isinstance(training_outcome, str) else None,
        "training_success": training_success,
        "training_failure_reasons": [],
        "holdout_failure_reasons": sorted(set(str(item) for item in holdout_failures)),
        "execution_order": execution_order,
        "funnel": {
            "supply": {
                "mining_reports": training_funnel["mining_reports"],
                "dbs": training_db_manifest,
            },
            "transport": {
                "dbs": transported_db_manifest,
                "intact": transport_intact,
            },
            "retrieval": {
                "entry_ids": sorted(retrieved_entry_ids),
                "transported_entry_ids_retrieved": sorted(
                    transported_entry_ids & retrieved_entry_ids
                ),
                "guidelines_retrieved": guidelines_retrieved,
            },
            "outcome": {
                "rubric_passed": rubric["passed"] if complete else None,
                "repair_retries": holdout_funnel["repair_retries"],
                "structured_output_retries": holdout_funnel[
                    "structured_output_retries"
                ],
                "provider_transient_retries": holdout_funnel[
                    "provider_transient_retries"
                ],
                "max_token_retries": holdout_funnel["max_token_retries"],
                "total_retries": holdout_funnel["total_retries"],
            },
        },
    }
    _private_write(
        attempt_root,
        processes,
        {
            **result,
            "training_wall_time_s": train_elapsed,
            "holdout_wall_time_s": holdout_elapsed,
            "training_session_id": train_payload.get("session_id"),
            "holdout_session_id": holdout_payload.get("session_id"),
            "structured_output": structured_output,
        },
    )
    return result


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _aggregate_condition(
    condition: str, replicas: int, runs: list[dict[str, object]]
) -> dict[str, object]:
    completed = [run for run in runs if run["holdout_complete"] is True]
    costs = [
        float(run["total_cost_usd"])
        for run in runs
        if isinstance(run["total_cost_usd"], (int, float))
    ]
    retrievals = [float(run["guidelines_retrieved"]) for run in runs]
    return {
        "condition": condition,
        "scheduled_replicas": replicas,
        "completion_rate": len(completed) / replicas,
        "holdout_rubric_pass_rate": (
            sum(run["rubric_passed"] is True for run in completed) / len(completed)
            if completed
            else None
        ),
        "median_total_cost_usd": statistics.median(costs) if costs else None,
        "mean_total_cost_usd": _mean(costs),
        "mean_guidelines_retrieved": _mean(retrievals),
    }


def _delta(memory: object, control: object) -> float | None:
    if not isinstance(memory, (int, float)) or not isinstance(control, (int, float)):
        return None
    return float(memory) - float(control)


def _comparison(aggregates: Mapping[str, dict[str, object]]) -> dict[str, float | None]:
    memory = aggregates.get("memory", {})
    control = aggregates.get("control", {})
    return {
        "rubric_pass_rate_delta": _delta(
            memory.get("holdout_rubric_pass_rate"), control.get("holdout_rubric_pass_rate")
        ),
        "mean_total_cost_usd_delta": _delta(
            memory.get("mean_total_cost_usd"), control.get("mean_total_cost_usd")
        ),
        "mean_guidelines_retrieved_delta": _delta(
            memory.get("mean_guidelines_retrieved"), control.get("mean_guidelines_retrieved")
        ),
    }


def _smoke_gate(runs: list[dict[str, object]], root_id: str) -> dict[str, object]:
    """Decide whether supply plumbing is sound enough to fund variant trials."""
    failures: list[str] = []
    memory_runs = [run for run in runs if run.get("condition") == "memory"]
    control_runs = [run for run in runs if run.get("condition") == "control"]
    if len(memory_runs) != 2 or len(control_runs) != 2:
        failures.append("requires_exactly_two_replicas_per_condition")
    for run in memory_runs:
        replica = run.get("replica")
        funnel = run.get("funnel")
        if not isinstance(funnel, dict):
            failures.append(f"memory_replica_{replica}_funnel_missing")
            continue
        supply = funnel.get("supply")
        transport = funnel.get("transport")
        retrieval = funnel.get("retrieval")
        reports = supply.get("mining_reports", []) if isinstance(supply, dict) else []
        specialist_entry_ids = {
            str(entry_id)
            for report in reports
            if isinstance(report, dict)
            and report.get("producer_agent_id") not in {None, root_id}
            for entry_id in report.get("entry_ids", [])
        }
        if not specialist_entry_ids:
            failures.append(f"memory_replica_{replica}_specialist_supply_missing")
        if not isinstance(transport, dict) or transport.get("intact") is not True:
            failures.append(f"memory_replica_{replica}_transport_failed")
        transported_retrieved = (
            retrieval.get("transported_entry_ids_retrieved", [])
            if isinstance(retrieval, dict) else []
        )
        if not specialist_entry_ids.intersection(map(str, transported_retrieved)):
            failures.append(f"memory_replica_{replica}_specialist_entry_not_retrieved")
    for run in control_runs:
        replica = run.get("replica")
        funnel = run.get("funnel")
        transport = funnel.get("transport") if isinstance(funnel, dict) else None
        dbs = transport.get("dbs", []) if isinstance(transport, dict) else []
        if any(isinstance(db, dict) and db.get("present") is True for db in dbs):
            failures.append(f"control_replica_{replica}_db_present")
        if run.get("guidelines_retrieved") != 0:
            failures.append(f"control_replica_{replica}_retrieval_nonzero")
    return {"go": not failures, "failures": sorted(set(failures))}


def _is_number(value: object, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def validate_memory_payload(payload: dict[str, object]) -> None:
    """Validate the public, prompt- and trace-free memory-study schema."""
    required: dict[str, tuple[type[object], ...]] = {
        "study": (str,),
        "generated_at": (str,),
        "case_id": (str,),
        "company": (str,),
        "fabri_version": (str, type(None)),
        "source": (dict,),
        "replicas": (int,),
        "conditions": (list,),
        "runs": (list,),
        "condition_aggregates": (dict,),
        "comparison": (dict,),
        "claim_boundary": (str,),
        "status": (str,),
        "smoke_gate": (dict,),
    }
    for key, types in required.items():
        if key not in payload:
            raise ProbeError(f"memory payload missing required key: {key}")
        if not isinstance(payload[key], types) or (
            key == "replicas" and isinstance(payload[key], bool)
        ):
            raise ProbeError(f"memory payload field {key!r} has an invalid type")
    if payload["study"] != "company-memory-vs-control":
        raise ProbeError("memory payload study has an invalid value")
    retrieval_overrides = payload.get("retrieval_overrides")
    if retrieval_overrides is not None:
        if not isinstance(retrieval_overrides, dict):
            raise ProbeError("memory payload retrieval_overrides has an invalid type")
        for key in ("top_k", "retrieval_strategy"):
            if key not in retrieval_overrides:
                raise ProbeError(f"memory payload retrieval_overrides missing key: {key}")
        top_k = retrieval_overrides.get("top_k")
        strategy = retrieval_overrides.get("retrieval_strategy")
        _validate_retrieval_overrides(top_k, strategy)
    if not isinstance(payload["replicas"], int) or payload["replicas"] < 1:
        raise ProbeError("memory payload replicas must be positive")
    conditions = payload["conditions"]
    if not isinstance(conditions, list) or not all(isinstance(item, str) for item in conditions):
        raise ProbeError("memory payload conditions must be a list of strings")
    source = payload["source"]
    if not isinstance(source, dict):
        raise ProbeError("memory payload source has an invalid type")
    for key, types in {
        "path": (str,),
        "roster_revision": (str, type(None)),
        "roster_worktree_clean": (bool, type(None)),
        "company_source_sha256": (str,),
    }.items():
        if key not in source or not isinstance(source[key], types):
            raise ProbeError(f"memory payload source field {key!r} has an invalid type")
    runs = payload["runs"]
    if not isinstance(runs, list):
        raise ProbeError("memory payload runs has an invalid type")
    run_fields: dict[str, tuple[type[object], ...]] = {
        "replica": (int,),
        "condition": (str,),
        "holdout_complete": (bool,),
        "rubric_passed": (bool, type(None)),
        "scoring_mode": (str,),
        "missing_required": (list,),
        "forbidden_hits": (list,),
        "total_cost_usd": (int, float, type(None)),
        "guidelines_retrieved": (int,),
        "retrieval_candidate_kinds": (list,),
        "training_outcome": (str, type(None)),
        "training_success": (bool, type(None)),
        "training_failure_reasons": (list,),
        "holdout_failure_reasons": (list,),
        "execution_order": (int,),
        "funnel": (dict,),
    }
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ProbeError(f"memory payload run {index} must be a mapping")
        for key, types in run_fields.items():
            if key not in run or not isinstance(run[key], types) or (
                key in {"replica", "guidelines_retrieved", "total_cost_usd"}
                and isinstance(run[key], bool)
            ):
                raise ProbeError(f"memory payload run {index} field {key!r} has an invalid type")
        if run["rubric_passed"] is not None and run["holdout_complete"] is not True:
            raise ProbeError(f"memory payload run {index} has a rubric verdict without completion")
        if run["scoring_mode"] != "structured":
            raise ProbeError(f"memory payload run {index} has an invalid scoring mode")
        if run["training_success"] is True and run["training_failure_reasons"]:
            raise ProbeError(
                f"memory payload run {index} reports training success with training failures"
            )
    aggregates = payload["condition_aggregates"]
    if not isinstance(aggregates, dict):
        raise ProbeError("memory payload condition_aggregates has an invalid type")
    aggregate_fields = {
        "condition": (str,),
        "scheduled_replicas": (int,),
        "completion_rate": (int, float),
        "holdout_rubric_pass_rate": (int, float, type(None)),
        "median_total_cost_usd": (int, float, type(None)),
        "mean_total_cost_usd": (int, float, type(None)),
        "mean_guidelines_retrieved": (int, float, type(None)),
    }
    for condition in conditions:
        aggregate = aggregates.get(condition)
        if not isinstance(aggregate, dict):
            raise ProbeError(f"memory payload aggregate missing condition: {condition}")
        for key, types in aggregate_fields.items():
            if key not in aggregate or not isinstance(aggregate[key], types) or (
                key != "condition" and isinstance(aggregate[key], bool)
            ):
                raise ProbeError(
                    f"memory payload aggregate {condition!r} field {key!r} has an invalid type"
                )
    comparison = payload["comparison"]
    if not isinstance(comparison, dict):
        raise ProbeError("memory payload comparison has an invalid type")
    for key in (
        "rubric_pass_rate_delta",
        "mean_total_cost_usd_delta",
        "mean_guidelines_retrieved_delta",
    ):
        if key not in comparison or not _is_number(comparison[key], nullable=True):
            raise ProbeError(f"memory payload comparison field {key!r} has an invalid type")


def render_markdown(payload: dict[str, object]) -> str:
    """Render the public summary from the already-validated public payload."""
    source = payload["source"]
    aggregates = payload["condition_aggregates"]
    comparison = payload["comparison"]
    if (
        not isinstance(source, dict)
        or not isinstance(aggregates, dict)
        or not isinstance(comparison, dict)
    ):
        raise ProbeError("memory payload has invalid markdown inputs")

    def display(value: object, format_spec: str, prefix: str = "") -> str:
        return "—" if value is None else f"{prefix}{float(value):{format_spec}}"

    retrieval_overrides = payload.get("retrieval_overrides")
    if isinstance(retrieval_overrides, dict):
        top_k = retrieval_overrides.get("top_k")
        strategy = retrieval_overrides.get("retrieval_strategy")
    else:
        top_k = None
        strategy = None

    lines = [
        "# Company memory vs control study",
        "",
        f"- Supply smoke gate: `{'GO' if isinstance(payload.get('smoke_gate'), dict) and payload['smoke_gate'].get('go') else 'STOP'}`",
        f"- Retrieval overrides: top_k=`{top_k}`, retrieval_strategy=`{strategy}`",
        f"- Case: `{payload['case_id']}`",
        f"- Company: `{payload['company']}`",
        f"- Fabri version: `{payload['fabri_version'] or 'unavailable'}`",
        f"- Roster revision: `{source.get('roster_revision') or 'unavailable'}`",
        f"- Company source SHA-256: `{source.get('company_source_sha256')}`",
        f"- Claim boundary: {payload['claim_boundary']}",
        "",
        "| Condition | Completion | Holdout rubric | Median cost | Mean cost | Mean guidelines |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    conditions = payload["conditions"]
    if isinstance(conditions, list):
        for condition in conditions:
            aggregate = aggregates.get(condition, {})
            if not isinstance(aggregate, dict):
                continue
            rubric = aggregate.get("holdout_rubric_pass_rate")
            median_cost = aggregate.get("median_total_cost_usd")
            mean_cost = aggregate.get("mean_total_cost_usd")
            mean_retrieval = aggregate.get("mean_guidelines_retrieved")
            lines.append(
                f"| {condition} | {float(aggregate.get('completion_rate', 0)):.0%} | "
                f"{display(rubric, '.0%')} | "
                f"{display(median_cost, '.4f', '$')} | "
                f"{display(mean_cost, '.4f', '$')} | "
                f"{display(mean_retrieval, '.2f')} |"
            )
    rubric_delta = comparison.get("rubric_pass_rate_delta")
    cost_delta = comparison.get("mean_total_cost_usd_delta")
    retrieval_delta = comparison.get("mean_guidelines_retrieved_delta")
    lines.extend([
        "",
        "| Memory − control | Rubric pass-rate delta | Mean cost delta | Mean guidelines delta |",
        "|---|---:|---:|---:|",
        "| Difference | "
        f"{display(rubric_delta, '+.1%')} | "
        f"{display(cost_delta, '+.4f', '$')} | "
        f"{display(retrieval_delta, '+.2f')} |",
        "",
        "Incomplete holdouts have no rubric verdict. Prompts, traces, session IDs, and raw "
        "model output remain under `private-attempts/`.",
    ])
    return "\n".join(lines) + "\n"


def run_memory_study(
    case: MemoryCase,
    output_dir: str | Path,
    *,
    command_runner: CommandRunner = _run_command,
    run_timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
    cwd: Path | None = None,
    retrieval_top_k: int | None = None,
    retrieval_strategy: str | None = None,
) -> dict[str, object]:
    """Run sequential, fresh-compile memory and control replicas and publish aggregates."""
    if run_timeout_s <= 0:
        raise ProbeError("run_timeout_s must be positive")
    _validate_retrieval_overrides(retrieval_top_k, retrieval_strategy)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work_root = output / "private-attempts"
    work_root.mkdir(parents=True, exist_ok=True)
    command_cwd = Path.cwd() if cwd is None else cwd
    environment = dict(os.environ)
    source_manifest = build_source_manifest(
        case, command_runner, command_cwd, environment=environment
    )
    runs: list[dict[str, object]] = []
    for replica in range(1, case.replicas + 1):
        condition_order = (
            case.conditions if replica % 2 == 1 else tuple(reversed(case.conditions))
        )
        for execution_order, condition in enumerate(condition_order, start=1):
            runs.append(
                _run_pair(
                    case,
                    replica,
                    condition,
                    work_root,
                    command_runner,
                    command_cwd,
                    environment,
                    run_timeout_s,
                    retrieval_top_k,
                    retrieval_strategy,
                    execution_order,
                )
            )
    aggregates = {
        condition: _aggregate_condition(
            condition, case.replicas, [run for run in runs if run["condition"] == condition]
        )
        for condition in case.conditions
    }
    try:
        fabri_version: str | None = importlib.metadata.version("fabri")
    except importlib.metadata.PackageNotFoundError:
        fabri_version = None
    payload: dict[str, object] = {
        "study": "company-memory-vs-control",
        "generated_at": datetime.now(UTC).isoformat(),
        "case_id": case.case_id,
        "company": case.company_name,
        "fabri_version": fabri_version,
        "source": source_manifest,
        "retrieval_overrides": {
            "top_k": retrieval_top_k,
            "retrieval_strategy": retrieval_strategy,
        },
        "replicas": case.replicas,
        "conditions": list(case.conditions),
        "runs": runs,
        "condition_aggregates": aggregates,
        "comparison": _comparison(aggregates),
        "smoke_gate": _smoke_gate(runs, case.root_id),
        "claim_boundary": CLAIM_BOUNDARY,
        "status": "completed",
    }
    validate_memory_payload(payload)
    (output / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output / "results.md").write_text(render_markdown(payload), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--replicas", type=int, default=None)
    parser.add_argument("--run-timeout-s", type=float, default=DEFAULT_RUN_TIMEOUT_S)
    parser.add_argument("--retrieval-top-k", type=int, default=None)
    parser.add_argument("--retrieval-strategy", default=None)
    args = parser.parse_args(argv)
    if args.run_timeout_s <= 0:
        parser.error("--run-timeout-s must be positive")
    try:
        _validate_retrieval_overrides(args.retrieval_top_k, args.retrieval_strategy)
        case = load_memory_case(args.dataset, args.case_id, replicas_override=args.replicas)
        result = run_memory_study(
            case,
            args.output_dir,
            run_timeout_s=args.run_timeout_s,
            retrieval_top_k=args.retrieval_top_k,
            retrieval_strategy=args.retrieval_strategy,
        )
    except (OSError, ProbeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": result["status"],
                "results": str(Path(args.output_dir).resolve() / "results.json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
