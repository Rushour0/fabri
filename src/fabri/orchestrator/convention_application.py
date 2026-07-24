"""Deterministic validation for applying retrieved response conventions."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fabri.memory.conventions import ConventionRecord
from fabri.memory.schema import MemoryEntry


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating one model-selected convention branch.

    ``selected_fields`` carries the branch's exact mapped values when the
    selection is valid, so the CALLER (engine) performs the deterministic
    copy. Five live smoke rounds showed that asking the model to both select
    a branch and hand-copy its values is the failure surface — selection is
    the model's judgment call; copying is mechanical and belongs to the
    engine.
    """

    valid: bool
    reason: str | None
    selected_branch_id: str | None
    convention_fields: frozenset[str]
    max_retries: int
    selected_fields: Mapping[str, object] | None = None


def _memory_config(config: object) -> object:
    if isinstance(config, Mapping):
        memory = config.get("memory")
        return memory if isinstance(memory, Mapping) else config
    return config


def _max_retries(config: object) -> int:
    memory = _memory_config(config)
    if isinstance(memory, Mapping):
        value = memory.get("convention_branch_selection_max_retries", 1)
    else:
        value = getattr(memory, "convention_branch_selection_max_retries", 1)
    if not isinstance(value, int) or isinstance(value, bool):
        return 1
    return max(0, value)


def _record_payload(convention: object) -> Mapping[str, object] | None:
    if isinstance(convention, ConventionRecord):
        return convention.to_dict()
    if isinstance(convention, MemoryEntry):
        record = convention.payload.get("record")
        return record if isinstance(record, Mapping) else None
    if not isinstance(convention, Mapping):
        return None
    record = convention.get("record")
    if isinstance(record, Mapping):
        return record
    return convention


def _branches(
    retrieved_conventions: Iterable[object],
) -> tuple[list[tuple[str, Mapping[str, object]]], frozenset[str]]:
    branches: list[tuple[str, Mapping[str, object]]] = []
    convention_fields: set[str] = set()
    for convention in retrieved_conventions:
        record = _record_payload(convention)
        if record is None:
            continue
        raw_branches = record.get("branches")
        if not isinstance(raw_branches, list):
            continue
        for branch in raw_branches:
            if not isinstance(branch, Mapping):
                continue
            branch_id = branch.get("branch_id")
            fields = branch.get("fields")
            if not isinstance(branch_id, str) or not isinstance(fields, Mapping):
                continue
            string_fields = {
                key: value for key, value in fields.items() if isinstance(key, str)
            }
            if len(string_fields) != len(fields):
                continue
            branches.append((branch_id, string_fields))
            convention_fields.update(string_fields)
    return branches, frozenset(convention_fields)


_MARKED_SELECTION_RE = re.compile(
    r"^\s*SELECTED_BRANCH\s*:\s*(?P<branch>\S+)\s*$", re.MULTILINE
)
_MARKED_EVIDENCE_RE = re.compile(
    r"^\s*BRANCH_EVIDENCE\s*:\s*(?P<evidence>\S.*)$", re.MULTILINE
)


def _marked_selections(structured_output: Mapping[str, object]) -> list[str]:
    """Selections declared as marked lines inside the response prose.

    Closed response schemas (benchmark holdouts validate `response` plus the
    mapped fields and nothing else) leave the model no legal field for
    `selected_branch_id` — a live smoke showed it declaring the correct branch
    in prose while the field-only validator read nothing. The marked-line
    channel is deterministic: one `SELECTED_BRANCH: <id>` line per selection.
    """
    response = structured_output.get("response")
    if not isinstance(response, str):
        return []
    return [m.group("branch") for m in _MARKED_SELECTION_RE.finditer(response)]


def _has_current_run_evidence(structured_output: Mapping[str, object]) -> bool:
    evidence = structured_output.get("current_run_evidence")
    if isinstance(evidence, str):
        return bool(evidence.strip())
    if isinstance(evidence, (Mapping, list, tuple, set)):
        return bool(evidence)
    if evidence is None:
        response = structured_output.get("response")
        if isinstance(response, str) and _MARKED_EVIDENCE_RE.search(response):
            return True
        return False
    return evidence is not False


def validate_branch_selection(
    structured_output: object,
    retrieved_conventions: Iterable[object],
    *,
    config: object,
) -> ValidationResult:
    """Accept only one evidenced branch with an exact response-field mapping."""
    branches, convention_fields = _branches(retrieved_conventions)
    retries = _max_retries(config)

    def invalid(reason: str, branch_id: str | None = None) -> ValidationResult:
        return ValidationResult(
            valid=False,
            reason=reason,
            selected_branch_id=branch_id,
            convention_fields=convention_fields,
            max_retries=retries,
        )

    if not isinstance(structured_output, Mapping):
        return invalid("structured_output_not_mapping")

    selected = structured_output.get("selected_branch_id")
    if isinstance(selected, (list, tuple, set)):
        return invalid("multiple_branch_selection")
    if selected is None:
        marked = _marked_selections(structured_output)
        if len(marked) > 1:
            return invalid("multiple_branch_selection")
        if marked:
            selected = marked[0]
    if not isinstance(selected, str) or not selected.strip():
        return invalid("selected_branch_id_missing")
    if "selected_branch_ids" in structured_output:
        return invalid("multiple_branch_selection", selected)

    matches = [fields for branch_id, fields in branches if branch_id == selected]
    if len(matches) != 1:
        return invalid("selected_branch_id_not_unique", selected)
    if not _has_current_run_evidence(structured_output):
        return invalid("current_run_evidence_missing", selected)

    # A unique, evidenced selection is sufficient: the engine copies the
    # branch's mapped values itself (see selected_fields), so hand-copy
    # fidelity in the model output is no longer a validity condition.
    return ValidationResult(
        valid=True,
        reason=None,
        selected_branch_id=selected,
        convention_fields=convention_fields,
        max_retries=retries,
        selected_fields=dict(matches[0]),
    )
