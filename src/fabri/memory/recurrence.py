"""Pure, fail-closed matching for typed ActionMemory recoveries.

An ActionMemory ``resolution`` is a dictionary with this shape::

    {
        "problem_signature": {
            "phase": str,
            "agency": str,
            "roles": list[str],
            "error_class": str,
            "cause": str,
            "configured_cap": int | None,
            "retry_cap": int | None,
            "model_family": str | None,
            "finish_reason": str | None,
        },
        "scope": {"company": str, "agency": str, "roles": list[str]},
        "preconditions": [
            {"field": "dotted.path.in.current_state", "equals": object},
        ],
        "steps": [{"capability": str, "args_template": dict}],
        "postconditions": list[dict],
        "rollback": dict | None,
        "evidence": {"source_session_ids": list[str], "verification": str, "replays": int},
        "policy": {"idempotent": bool, "max_attempts": int, "approval": str, "expiry": object},
    }

Only equality preconditions are supported in this increment.  A precondition's
``field`` is a dotted path into ``current_state`` and its ``equals`` value must
match exactly.  Invalid or incomplete input is never an exception: it is not
applicable.  The functions do no I/O and have no dependency on a memory store.

``apply_confidence`` deliberately is not a retrieval score.  An exact
fingerprint with satisfied hard checks scores ``0.90..1.00``; a configuration-
applicable structured near match scores ``0.45..0.60``; an inapplicable result
can receive at most ``0.10`` in proportion to its structured similarity.
"""
from __future__ import annotations

import hashlib
from typing import NotRequired, TypedDict


class ProblemSignature(TypedDict):
    """Stable facts that identify a recurring operational problem."""

    phase: str | None
    agency: str | None
    roles: list[str]
    error_class: str | None
    cause: str | None
    configured_cap: int | None
    retry_cap: int | None
    model_family: str | None
    finish_reason: str | None


class Scope(TypedDict):
    """The company, agency, and roles to which a resolution is restricted."""

    company: str
    agency: str
    roles: list[str]


class Precondition(TypedDict):
    """An equality predicate over a dotted path in current state."""

    field: str
    equals: object
    op: NotRequired[str]


class ActionMemoryResolution(TypedDict):
    """Stored, typed recovery data for a previously verified incident."""

    problem_signature: ProblemSignature
    scope: Scope
    preconditions: list[Precondition]
    steps: list[dict[str, object]]
    postconditions: list[dict[str, object]]
    rollback: dict[str, object] | None
    evidence: dict[str, object]
    policy: dict[str, object]


_SIGNATURE_FIELDS = (
    "phase",
    "agency",
    "roles",
    "error_class",
    "cause",
    "configured_cap",
    "retry_cap",
    "model_family",
    "finish_reason",
)
_MISSING = object()


def canonicalize(signals: dict[str, object]) -> dict[str, object]:
    """Return only deterministic, stable problem-signature fields.

    Callers may provide a raw signals mapping or one nested beneath
    ``problem_signature``.  Everything outside the explicit field whitelist
    (session IDs, timestamps, paths, prices, and free-form text) is discarded.
    """
    nested = signals.get("problem_signature")
    source = nested if isinstance(nested, dict) else signals
    roles = source.get("roles")
    canonical_roles = (
        sorted({role for role in roles if isinstance(role, str)})
        if isinstance(roles, (list, tuple))
        else []
    )
    signature: dict[str, object] = {"roles": canonical_roles}
    for field in _SIGNATURE_FIELDS:
        if field == "roles":
            continue
        value = source.get(field)
        if field in {"configured_cap", "retry_cap"}:
            signature[field] = value if isinstance(value, int) and not isinstance(value, bool) else None
        else:
            signature[field] = value if isinstance(value, str) else None
    return signature


def fingerprint(signature: dict[str, object]) -> str:
    """Hash canonical stable fields in a fixed order into a SHA-256 digest."""
    canonical = canonicalize(signature)
    encoded_fields = [
        f"{field}:{_encode_value(canonical[field])}" for field in _SIGNATURE_FIELDS
    ]
    return hashlib.sha256("|".join(encoded_fields).encode("utf-8")).hexdigest()


def applicable(resolution: dict[str, object], current_state: dict[str, object]) -> bool:
    """Return whether a resolution's scope, bad cap, and predicates still hold.

    The scope's company and agency must match, every target role must be in
    ``roles_config``, and each role must still have the failing configured cap.
    Missing or malformed data fails closed.
    """
    signature = resolution.get("problem_signature")
    scope = resolution.get("scope")
    preconditions = resolution.get("preconditions")
    if not isinstance(signature, dict) or not isinstance(scope, dict) or not isinstance(preconditions, list):
        return False

    company = scope.get("company")
    agency = scope.get("agency")
    roles = scope.get("roles")
    if (
        not isinstance(company, str)
        or not isinstance(agency, str)
        or not isinstance(roles, list)
        or not roles
        or any(not isinstance(role, str) for role in roles)
        or current_state.get("company") != company
        or current_state.get("agency") != agency
    ):
        return False

    configured_cap = signature.get("configured_cap")
    roles_config = current_state.get("roles_config")
    if (
        not isinstance(configured_cap, int)
        or isinstance(configured_cap, bool)
        or not isinstance(roles_config, dict)
    ):
        return False
    for role in roles:
        role_config = roles_config.get(role)
        if not isinstance(role_config, dict) or role_config.get("max_tokens") != configured_cap:
            return False

    return all(_precondition_matches(precondition, current_state) for precondition in preconditions)


def apply_confidence(
    resolution: dict[str, object], current_state: dict[str, object], retrieval_relevance: float
) -> float:
    """Score execution confidence separately from vector retrieval relevance.

    Exact signature recurrence plus applicability is required for high
    confidence.  Retrieval relevance only adjusts a bounded score band; it
    cannot make an inapplicable recovery trustworthy.
    """
    relevance = min(1.0, max(0.0, retrieval_relevance))
    signature = resolution.get("problem_signature")
    if not isinstance(signature, dict):
        return 0.0

    canonical_signature = canonicalize(signature)
    canonical_current = canonicalize(current_state)
    exact_match = fingerprint(canonical_signature) == fingerprint(canonical_current)
    is_applicable = applicable(resolution, current_state)
    if exact_match and is_applicable:
        return 0.90 + 0.10 * relevance
    if is_applicable:
        return 0.45 + 0.15 * relevance

    return 0.10 * relevance * _structured_similarity(canonical_signature, canonical_current)


def _encode_value(value: object) -> str:
    """Use length prefixes so the fixed-order fingerprint serialization is unambiguous."""
    if value is None:
        return "none"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"int:{value}"
    if isinstance(value, str):
        return f"str:{len(value)}:{value}"
    if isinstance(value, list):
        return "list:" + ",".join(_encode_value(item) for item in value)
    return "invalid"


def _precondition_matches(precondition: object, current_state: dict[str, object]) -> bool:
    if not isinstance(precondition, dict):
        return False
    field = precondition.get("field")
    if not isinstance(field, str) or "equals" not in precondition:
        return False
    if precondition.get("op", "equals") != "equals":
        return False
    value = _read_path(current_state, field)
    return value is not _MISSING and value == precondition["equals"]


def _read_path(state: dict[str, object], path: str) -> object:
    value: object = state
    for component in path.split("."):
        if not component or not isinstance(value, dict) or component not in value:
            return _MISSING
        value = value[component]
    return value


def _structured_similarity(left: dict[str, object], right: dict[str, object]) -> float:
    """Compare available stable fields without treating missing values as matches."""
    comparable = [
        field
        for field in _SIGNATURE_FIELDS
        if left[field] is not None and right[field] is not None
    ]
    if not comparable:
        return 0.0
    matches = sum(left[field] == right[field] for field in comparable)
    return matches / len(comparable)
