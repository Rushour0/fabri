"""Deterministic, fail-closed convention records and ingestion."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from fabri.memory.compress import count_tokens
from fabri.memory.schema import MemoryEntry

_CONFIG_ORIGINS = frozenset({"company_config", "agency_config"})
_MINED_ORIGINS = frozenset({"task", "model", "tool"})
_ORIGINS = _CONFIG_ORIGINS | _MINED_ORIGINS
_SCOPES = frozenset({"agent", "agency", "company"})
_SCOPE_BREADTH = {"agent": 0, "agency": 1, "company": 2}
_SOURCE_MAX_SCOPE = {"agency_config": "agency", "company_config": "company"}
_HARD_ALLOWED_EFFECT_CLASSES = frozenset({"response_mapping"})
_ACTIVE_TIERS = frozenset({"retrieve", "core"})
_INGEST_LOCK = threading.RLock()

_PROHIBITED_BRANCH_CONTENT = re.compile(
    r"""
    (?:
        \btool[\s_-]*(?:call|use|invocation)\b
        |"tool"\s*:
        |\b(?:call|invoke|run|execute)[\s_-]+(?:a[\s_-]+)?tool\b
        |\bfunction[\s_-]*call\b
        |\bspawn[\s_-]*subagent\b
        |\bdelegat(?:e|ion|ing)\b
        |\bhand[\s_-]*off\b
        |\b(?:company|agency|agent)[\s_-]*config\b
        |\bconfig(?:uration)?[\s_-]*(?:path|file|write|edit|update)\b
        |(?:^|[\s"'=:/\\])(?:\.{0,2}/|~?/|[a-zA-Z]:\\)[^\s"']+
        |\.(?:ya?ml|toml|ini|cfg|conf|env)(?:\b|$)
        |\b(?:credential|password|passwd|api[\s_-]*key|access[\s_-]*token|secret)\b
        |\b(?:read|write|create|delete|remove|mutate|edit)[\s_-]*(?:a[\s_-]+)?file\b
        |\bfile[\s_-]*(?:mutation|write|delete|creation|path)\b
        |\bfilesystem\b
        |https?://
        |\b(?:network[\s_-]*(?:call|request|action)|webhook|curl|wget)\b
        |\b(?:bypass|skip|disable|override|ignore)[\s_-]*(?:the[\s_-]+)?approval\b
        |\bapproval[\s_-]*bypass\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class ConventionStore(Protocol):
    """Minimal store surface required by convention ingestion."""

    def upsert(self, entry: MemoryEntry) -> str: ...

    def delete(self, point_id: str) -> None: ...

    def iterate(self, kind: str | None = None, limit: int | None = None) -> list[MemoryEntry]: ...


@dataclass
class ConventionRecord:
    """One atomic conditional response mapping."""

    scope: str
    key: str
    version: str
    effect_class: str
    conditions: list[dict[str, object]]
    branches: list[dict[str, object]]
    origin: str
    provenance: str
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    supersedes: str | None = None
    response_schema: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return the complete JSON-serializable record."""
        return {
            "scope": self.scope,
            "key": self.key,
            "version": self.version,
            "effect_class": self.effect_class,
            "conditions": self.conditions,
            "branches": self.branches,
            "origin": self.origin,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "supersedes": self.supersedes,
            "response_schema": self.response_schema,
        }

    def canonical_json(self) -> str:
        """Serialize the complete record with stable key and whitespace rules."""
        return canonical_serialize(self.to_dict())

    @property
    def branch_mapping_hash(self) -> str:
        """Hash only conditions and branches, invalidating any mapping change."""
        mapping = {"conditions": self.conditions, "branches": self.branches}
        return hashlib.sha256(canonical_serialize(mapping).encode("utf-8")).hexdigest()

    @property
    def approval_key(self) -> tuple[str, str, str, str]:
        """Return the exact identity a human approval must match."""
        return (self.scope, self.key, self.version, self.branch_mapping_hash)


def canonical_serialize(value: object) -> str:
    """Return deterministic UTF-8 JSON without insignificant whitespace."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def branch_mapping_hash(record: ConventionRecord) -> str:
    """Return the record's exact conditions-and-branches mapping hash."""
    return record.branch_mapping_hash


def render_convention(record: ConventionRecord) -> str:
    """Render the whole atomic record; this representation is never truncated."""
    return record.canonical_json()


def count_convention_tokens(record: ConventionRecord) -> int:
    """Count the complete deterministic rendering."""
    return count_tokens(render_convention(record))


def _memory_config(config: Mapping[str, object]) -> Mapping[str, object]:
    memory = config.get("memory")
    return memory if isinstance(memory, Mapping) else config


def _configured_list(config: Mapping[str, object], key: str) -> list[object]:
    value = config.get(key, [])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _approval_matches(record: ConventionRecord, approvals: list[object]) -> bool:
    expected = record.approval_key
    for approval in approvals:
        if isinstance(approval, Mapping):
            candidate = (
                approval.get("scope"),
                approval.get("key"),
                approval.get("version"),
                approval.get("branch_mapping_hash"),
            )
        elif isinstance(approval, Sequence) and not isinstance(approval, (str, bytes)):
            candidate = tuple(approval)
        else:
            continue
        if candidate == expected:
            return True
    return False


def _config_scope_allowed(origin: str, scope: str) -> bool:
    maximum = _SOURCE_MAX_SCOPE.get(origin)
    if maximum is None or scope not in _SCOPE_BREADTH:
        return False
    return _SCOPE_BREADTH[scope] <= _SCOPE_BREADTH[maximum]


def _branch_value_prohibited(value: object) -> bool:
    try:
        rendered = canonical_serialize(value)
    except (TypeError, ValueError):
        return True
    normalized = rendered.replace("_", " ").replace("-", " ")
    return _PROHIBITED_BRANCH_CONTENT.search(normalized) is not None


def convention_quarantine_reason(
    record: ConventionRecord,
    *,
    config: Mapping[str, object],
) -> str | None:
    """Validate one whole candidate and return its fail-closed reason, if any."""
    memory = _memory_config(config)
    allowed_effects = set(_configured_list(memory, "convention_allowed_effect_classes"))
    if (
        record.effect_class not in _HARD_ALLOWED_EFFECT_CLASSES
        or record.effect_class not in allowed_effects
    ):
        return "effect_class_not_allowed"
    if record.origin not in _ORIGINS or record.scope not in _SCOPES:
        return "invalid_record"

    max_branches = int(memory.get("convention_max_branches", 8))
    if not 2 <= len(record.branches) <= max_branches:
        return "branch_count_out_of_bounds"

    condition_ids: list[object] = []
    for condition in record.conditions:
        if set(condition) != {"branch_id", "condition_text"}:
            return "invalid_branch_mapping"
        branch_id = condition.get("branch_id")
        condition_text = condition.get("condition_text")
        if not isinstance(branch_id, str) or not isinstance(condition_text, str):
            return "invalid_branch_mapping"
        condition_ids.append(branch_id)

    branch_ids: list[object] = []
    declared_fields = set(record.response_schema)
    for branch in record.branches:
        if set(branch) != {"branch_id", "fields"}:
            return "invalid_branch_mapping"
        branch_id = branch.get("branch_id")
        fields = branch.get("fields")
        if not isinstance(branch_id, str) or not isinstance(fields, Mapping):
            return "invalid_branch_mapping"
        if not set(fields).issubset(declared_fields):
            return "effect_class_not_allowed"
        if any(_branch_value_prohibited(value) for value in fields.values()):
            return "effect_class_not_allowed"
        branch_ids.append(branch_id)

    if (
        len(condition_ids) != len(set(condition_ids))
        or len(branch_ids) != len(set(branch_ids))
        or set(condition_ids) != set(branch_ids)
    ):
        return "invalid_branch_mapping"

    max_tokens = int(memory.get("convention_max_tokens", 384))
    try:
        token_count = count_convention_tokens(record)
    except (TypeError, ValueError):
        return "invalid_record"
    if token_count > max_tokens:
        return "token_budget_exceeded"
    return None


def _stored_record(entry: MemoryEntry) -> Mapping[str, object] | None:
    value = entry.payload.get("record")
    return value if isinstance(value, Mapping) else None


def _record_expired(record: Mapping[str, object], now: float) -> bool:
    expires_at = record.get("expires_at")
    return (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and float(expires_at) <= now
    )


def _is_superseded(entry: MemoryEntry) -> bool:
    return entry.payload.get("status") == "superseded"


def _active_conventions(
    entries: list[MemoryEntry],
    *,
    scope: str,
    now: float,
) -> list[MemoryEntry]:
    active: list[MemoryEntry] = []
    for entry in entries:
        record = _stored_record(entry)
        if (
            entry.kind == "convention"
            and entry.scope == scope
            and entry.tier in _ACTIVE_TIERS
            and record is not None
            and not _record_expired(record, now)
            and not _is_superseded(entry)
        ):
            active.append(entry)
    return active


def _drop_inactive(
    store: ConventionStore,
    entries: list[MemoryEntry],
    *,
    scope: str,
    now: float,
) -> list[MemoryEntry]:
    retained: list[MemoryEntry] = []
    for entry in entries:
        record = _stored_record(entry)
        should_drop = (
            entry.kind == "convention"
            and entry.scope == scope
            and record is not None
            and (_record_expired(record, now) or _is_superseded(entry))
        )
        if should_drop:
            store.delete(entry.id)
        else:
            retained.append(entry)
    return retained


def _supersession_target(
    entries: list[MemoryEntry],
    record: ConventionRecord,
    *,
    now: float,
) -> tuple[MemoryEntry, Mapping[str, object]] | None:
    matches: list[tuple[MemoryEntry, Mapping[str, object]]] = []
    for entry in _active_conventions(entries, scope=record.scope, now=now):
        stored = _stored_record(entry)
        if stored is None:
            continue
        target_matches = (
            stored.get("version") == record.supersedes or entry.id == record.supersedes
        )
        if (
            target_matches
            and stored.get("scope") == record.scope
            and stored.get("key") == record.key
        ):
            matches.append((entry, stored))
    return matches[0] if len(matches) == 1 else None


def _entry_for(
    record: ConventionRecord,
    *,
    verification: str,
    tier: str,
    reason: str | None,
) -> MemoryEntry:
    payload: dict[str, object] = {
        "record": record.to_dict(),
        "branch_mapping_hash": record.branch_mapping_hash,
        "approval_key": list(record.approval_key),
        "status": "active" if tier in _ACTIVE_TIERS else "quarantine",
    }
    if reason is not None:
        payload["quarantine_reason"] = reason
    return MemoryEntry(
        text=render_convention(record),
        kind="convention",
        created_at=record.created_at,
        scope=record.scope,
        verification=verification,
        tier=tier,
        resolution=None,
        payload=payload,
    )


def ingest_convention(
    store: ConventionStore,
    record: ConventionRecord,
    *,
    config: Mapping[str, object],
) -> MemoryEntry:
    """Validate and atomically admit or quarantine one convention candidate."""
    memory = _memory_config(config)
    reason = convention_quarantine_reason(record, config=memory)
    approvals = _configured_list(memory, "convention_approvals")
    human_approved = reason is None and _approval_matches(record, approvals)
    trusted_sources = set(_configured_list(memory, "convention_trusted_sources"))
    config_verified = (
        record.origin in _CONFIG_ORIGINS
        and record.origin in trusted_sources
        and _config_scope_allowed(record.origin, record.scope)
    )

    verification = "unverified"
    tier = "quarantine"
    authority: str | None = None
    if reason is None and not bool(memory.get("convention_mining_enabled", False)):
        reason = "convention_mining_disabled"
    if reason is None and human_approved:
        verification = "human_verified"
        tier = "retrieve"
        authority = "human"
    elif reason is None and config_verified:
        verification = "config_verified"
        tier = "retrieve"
        authority = "config"
    elif reason is None:
        if record.origin in _CONFIG_ORIGINS:
            reason = "untrusted_config_source"
        else:
            reason = "approval_required"

    effective = record
    now = time.time()
    if tier == "retrieve" and effective.expires_at is None:
        ttl_days = float(memory.get("convention_default_ttl_days", 180))
        effective = replace(effective, expires_at=now + ttl_days * 86400)
        ttl_reason = convention_quarantine_reason(effective, config=memory)
        if ttl_reason is not None:
            reason = ttl_reason
            verification = "unverified"
            tier = "quarantine"
            authority = None
            effective = record
    elif tier == "retrieve" and effective.expires_at <= now:
        reason = "expired"
        verification = "unverified"
        tier = "quarantine"
        authority = None

    with _INGEST_LOCK:
        entries = store.iterate(kind="convention")
        if tier == "retrieve":
            entries = _drop_inactive(
                store,
                entries,
                scope=effective.scope,
                now=now,
            )

        target: tuple[MemoryEntry, Mapping[str, object]] | None = None
        if (
            effective.supersedes is not None
            and reason in {None, "approval_required", "untrusted_config_source"}
        ):
            target = _supersession_target(entries, effective, now=now)
            authorized_supersession = authority in {"human", "config"}
            issuer_mismatch = False
            if authority == "config" and target is not None:
                _, stored = target
                issuer_mismatch = not (
                    stored.get("origin") == effective.origin
                    and stored.get("provenance") == effective.provenance
                )
            target_version = target[1].get("version") if target is not None else None
            if tier != "retrieve" or not authorized_supersession or issuer_mismatch:
                reason = "supersession_not_authorized"
                verification = "unverified"
                tier = "quarantine"
                target = None
            elif target is None or effective.version == target_version:
                reason = "supersession_conflict"
                verification = "unverified"
                tier = "quarantine"
                target = None

        if tier == "retrieve":
            active = _active_conventions(entries, scope=effective.scope, now=now)
            replaced_id = target[0].id if target is not None else None
            active_ids = {
                entry.id for entry in active if entry.id not in {replaced_id}
            }
            candidate = _entry_for(
                effective,
                verification=verification,
                tier=tier,
                reason=None,
            )
            max_entries = int(memory.get("convention_max_entries", 256))
            if candidate.id not in active_ids and len(active_ids) >= max_entries:
                reason = "quota_exceeded"
                verification = "unverified"
                tier = "quarantine"
                target = None

        entry = _entry_for(
            effective,
            verification=verification,
            tier=tier,
            reason=reason,
        )
        store.upsert(entry)
        if target is not None and tier == "retrieve":
            store.delete(target[0].id)
        return entry
