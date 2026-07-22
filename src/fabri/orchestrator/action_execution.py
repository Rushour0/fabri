"""Fail-closed execution for the narrow ActionMemory capability allowlist."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


class ActionExecutionError(ValueError):
    """A remembered action failed validation and was not applied."""


@dataclass(frozen=True)
class AppliedMemoryAction:
    """One compiled role configuration changed by an ActionMemory recovery."""

    role: str
    config_path: str
    previous_max_tokens: int
    new_max_tokens: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _PlannedChange:
    role: str
    path: Path
    original_text: str
    config: dict[str, object]
    previous_max_tokens: int
    new_max_tokens: int


def _role_paths(
    agents_entries: Iterable[Mapping[str, object]],
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for entry in agents_entries:
        role = entry.get("name")
        config_path = entry.get("config")
        if not isinstance(role, str) or not role or not isinstance(config_path, str):
            continue
        if role in paths:
            raise ActionExecutionError(f"duplicate delegated role: {role}")
        paths[role] = Path(config_path)
    return paths


def _load_raw_config(path: Path) -> tuple[str, dict[str, object]]:
    try:
        original = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(original)
    except OSError as exc:
        raise ActionExecutionError(f"could not read delegated role config: {path}") from exc
    except yaml.YAMLError as exc:
        raise ActionExecutionError(f"malformed delegated role config: {path}") from exc
    if not isinstance(loaded, dict):
        raise ActionExecutionError(f"delegated role config must be a mapping: {path}")
    return original, loaded


def _plan_resolution(
    resolution: Mapping[str, object],
    role_paths: Mapping[str, Path],
) -> list[_PlannedChange]:
    signature = resolution.get("problem_signature")
    scope = resolution.get("scope")
    steps = resolution.get("steps")
    policy = resolution.get("policy")
    if not all(isinstance(item, Mapping) for item in (signature, scope, policy)):
        raise ActionExecutionError("action is missing signature, scope, or policy")
    if not isinstance(steps, list) or not steps:
        raise ActionExecutionError("action has no executable steps")
    assert isinstance(signature, Mapping)
    assert isinstance(scope, Mapping)
    assert isinstance(policy, Mapping)
    if policy.get("idempotent") is not True or policy.get("max_attempts") != 1:
        raise ActionExecutionError("action policy is not safe for automatic execution")

    configured_cap = signature.get("configured_cap")
    retry_cap = signature.get("retry_cap")
    roles = scope.get("roles")
    if (
        not isinstance(configured_cap, int)
        or isinstance(configured_cap, bool)
        or not isinstance(retry_cap, int)
        or isinstance(retry_cap, bool)
        or not isinstance(roles, list)
        or any(not isinstance(role, str) for role in roles)
    ):
        raise ActionExecutionError("action signature or role scope is malformed")
    if retry_cap <= configured_cap or retry_cap > configured_cap * 2 or retry_cap > 32_768:
        raise ActionExecutionError("token-cap action exceeds the safe increase boundary")

    planned: list[_PlannedChange] = []
    seen_roles: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping) or step.get("capability") != "configure_role":
            raise ActionExecutionError("unsupported ActionMemory capability")
        arguments = step.get("args_template")
        if not isinstance(arguments, Mapping):
            raise ActionExecutionError("configure_role arguments are malformed")
        role = arguments.get("role")
        target = arguments.get("max_tokens")
        if (
            not isinstance(role, str)
            or role not in roles
            or role in seen_roles
            or not isinstance(target, int)
            or isinstance(target, bool)
            or target != retry_cap
        ):
            raise ActionExecutionError("configure_role step does not match its signature")
        path = role_paths.get(role)
        if path is None:
            raise ActionExecutionError(f"action targets an unknown delegated role: {role}")
        original, config = _load_raw_config(path)
        llm = config.get("llm")
        if not isinstance(llm, dict) or llm.get("max_tokens") != configured_cap:
            raise ActionExecutionError(f"role {role} no longer has the failing token cap")
        llm["max_tokens"] = target
        planned.append(
            _PlannedChange(
                role=role,
                path=path,
                original_text=original,
                config=config,
                previous_max_tokens=configured_cap,
                new_max_tokens=target,
            )
        )
        seen_roles.add(role)
    return planned


def _atomic_write_yaml(path: Path, config: Mapping[str, object]) -> None:
    payload = yaml.safe_dump(dict(config), sort_keys=False, allow_unicode=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.action-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def apply_safe_memory_actions(
    resolutions: Iterable[Mapping[str, object]],
    agents_entries: Iterable[Mapping[str, object]],
) -> list[AppliedMemoryAction]:
    """Apply validated token-cap recoveries, rolling back on any write failure.

    Action data can select only a role name.  The trusted manager configuration
    supplies the corresponding file path, and every change must be a one-step,
    at-most-2x increase to the exact retry cap recorded by the failure.
    """
    role_paths = _role_paths(agents_entries)
    raw_planned = [
        change
        for resolution in resolutions
        for change in _plan_resolution(resolution, role_paths)
    ]
    planned_by_role: dict[str, _PlannedChange] = {}
    for change in raw_planned:
        existing = planned_by_role.get(change.role)
        if existing is None:
            planned_by_role[change.role] = change
            continue
        if existing.new_max_tokens != change.new_max_tokens:
            raise ActionExecutionError(
                f"conflicting actions target delegated role: {change.role}"
            )
    planned = list(planned_by_role.values())
    written: list[_PlannedChange] = []
    try:
        for change in planned:
            _atomic_write_yaml(change.path, change.config)
            written.append(change)
    except OSError as exc:
        for change in reversed(written):
            try:
                change.path.write_text(change.original_text, encoding="utf-8")
            except OSError:
                pass
        raise ActionExecutionError("failed to persist ActionMemory changes; rollback attempted") from exc
    return [
        AppliedMemoryAction(
            role=change.role,
            config_path=str(change.path),
            previous_max_tokens=change.previous_max_tokens,
            new_max_tokens=change.new_max_tokens,
        )
        for change in planned
    ]
