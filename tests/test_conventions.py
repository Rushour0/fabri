from __future__ import annotations

import time

import pytest

from fabri.memory.conventions import ConventionRecord, ingest_convention
from fabri.memory.schema import MemoryEntry
from fabri.memory.verification import verification_allowed

pytestmark = pytest.mark.unit


class MemoryStore:
    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self.entries = {entry.id: entry for entry in entries or []}

    def upsert(self, entry: MemoryEntry) -> str:
        self.entries[entry.id] = entry
        return entry.id

    def delete(self, point_id: str) -> None:
        self.entries.pop(point_id, None)

    def iterate(self, kind: str | None = None, limit: int | None = None) -> list[MemoryEntry]:
        entries = [
            entry for entry in self.entries.values()
            if kind is None or entry.kind == kind
        ]
        return entries[:limit] if limit is not None else entries


def _config(**overrides: object) -> dict[str, object]:
    memory: dict[str, object] = {
        "convention_mining_enabled": True,
        "convention_trusted_sources": [],
        "convention_approvals": [],
        "convention_allowed_effect_classes": ["response_mapping"],
        "convention_max_tokens": 384,
        "convention_max_branches": 8,
        "convention_max_entries": 256,
        "convention_default_ttl_days": 180,
    }
    memory.update(overrides)
    return {"memory": memory}


def _record(
    *,
    version: str = "1",
    origin: str = "task",
    scope: str = "agent",
    provenance: str = "session:s-1",
    value_a: object = "external",
    value_b: object = "internal",
    branch_count: int = 2,
    supersedes: str | None = None,
) -> ConventionRecord:
    conditions = [
        {"branch_id": f"branch-{index}", "condition_text": f"condition {index}"}
        for index in range(branch_count)
    ]
    branches = [
        {
            "branch_id": f"branch-{index}",
            "fields": {
                "status": value_a if index == 0 else (
                    value_b if index == 1 else f"value-{index}"
                )
            },
        }
        for index in range(branch_count)
    ]
    return ConventionRecord(
        scope=scope,
        key="incident-status",
        version=version,
        effect_class="response_mapping",
        conditions=conditions,
        branches=branches,
        origin=origin,
        provenance=provenance,
        response_schema={"status": "string"},
        supersedes=supersedes,
    )


def _approval(record: ConventionRecord) -> dict[str, str]:
    scope, key, version, mapping_hash = record.approval_key
    return {
        "scope": scope,
        "key": key,
        "version": version,
        "branch_mapping_hash": mapping_hash,
    }


def test_branch_mapping_hash_changes_when_any_branch_value_changes() -> None:
    original = _record(value_b="internal")
    changed = _record(value_b="monitor")

    assert original.branch_mapping_hash != changed.branch_mapping_hash


def test_task_origin_is_always_quarantined_despite_success_metadata() -> None:
    record = _record(provenance="session:succeeded-and-rubric-verified")

    entry = ingest_convention(
        MemoryStore(),
        record,
        config=_config(
            successful_session=True,
            session_verification="rubric_verified",
            hit_count=100,
        ),
    )

    assert entry.verification == "unverified"
    assert entry.tier == "quarantine"
    assert entry.resolution is None
    assert entry.payload["quarantine_reason"] == "approval_required"


def test_approval_flips_only_the_exact_version_and_mapping_hash() -> None:
    approved = _record()
    config = _config(convention_approvals=[_approval(approved)])

    exact = ingest_convention(MemoryStore(), approved, config=config)
    near_version = ingest_convention(
        MemoryStore(),
        _record(version="2"),
        config=config,
    )
    near_hash = ingest_convention(
        MemoryStore(),
        _record(value_b="monitor"),
        config=config,
    )

    assert (exact.verification, exact.tier) == ("human_verified", "retrieve")
    assert (near_version.verification, near_version.tier) == ("unverified", "quarantine")
    assert (near_hash.verification, near_hash.tier) == ("unverified", "quarantine")


@pytest.mark.parametrize(
    ("origin", "scope", "trusted_sources", "expected"),
    [
        ("company_config", "company", ["company_config"], ("config_verified", "retrieve")),
        ("agency_config", "agency", ["agency_config"], ("config_verified", "retrieve")),
        ("agency_config", "company", ["agency_config"], ("unverified", "quarantine")),
        ("company_config", "company", [], ("unverified", "quarantine")),
    ],
)
def test_config_origin_requires_trusted_source_and_scope_containment(
    origin: str,
    scope: str,
    trusted_sources: list[str],
    expected: tuple[str, str],
) -> None:
    entry = ingest_convention(
        MemoryStore(),
        _record(origin=origin, scope=scope, provenance=f"/configs/{origin}.yaml"),
        config=_config(convention_trusted_sources=trusted_sources),
    )

    assert (entry.verification, entry.tier) == expected


def test_tool_call_looking_branch_value_is_rejected_by_effect_class() -> None:
    record = _record(value_b={"tool_call": "send_email"})

    entry = ingest_convention(
        MemoryStore(),
        record,
        config=_config(convention_approvals=[_approval(record)]),
    )

    assert entry.tier == "quarantine"
    assert entry.payload["quarantine_reason"] == "effect_class_not_allowed"


def test_overflow_is_quarantined_whole_not_truncated() -> None:
    oversized = "bounded response " * 500
    record = _record(value_b=oversized)

    entry = ingest_convention(
        MemoryStore(),
        record,
        config=_config(convention_approvals=[_approval(record)]),
    )

    stored = entry.payload["record"]
    assert isinstance(stored, dict)
    assert entry.tier == "quarantine"
    assert entry.payload["quarantine_reason"] == "token_budget_exceeded"
    assert stored["branches"][1]["fields"]["status"] == oversized
    assert "..." not in stored["branches"][1]["fields"]["status"]


@pytest.mark.parametrize("branch_count", [1, 9])
def test_branch_count_outside_bounds_is_rejected(branch_count: int) -> None:
    record = _record(branch_count=branch_count)

    entry = ingest_convention(
        MemoryStore(),
        record,
        config=_config(convention_approvals=[_approval(record)]),
    )

    assert entry.tier == "quarantine"
    assert entry.payload["quarantine_reason"] == "branch_count_out_of_bounds"
    assert len(entry.payload["record"]["branches"]) == branch_count


def test_quota_drops_expired_before_refusing_active_eviction() -> None:
    store = MemoryStore()
    config = _config(convention_trusted_sources=["company_config"])
    active: list[MemoryEntry] = []
    for index in range(256):
        active.append(ingest_convention(
            store,
            _record(
                version=str(index),
                origin="company_config",
                scope="company",
                provenance="/configs/company.yaml",
            ),
            config=config,
        ))

    expired = active[0]
    expired.payload["record"]["expires_at"] = 0.0
    store.upsert(expired)
    admitted = ingest_convention(
        store,
        _record(
            version="replacement",
            origin="company_config",
            scope="company",
            provenance="/configs/company.yaml",
        ),
        config=config,
    )
    refused = ingest_convention(
        store,
        _record(
            version="over-quota",
            origin="company_config",
            scope="company",
            provenance="/configs/company.yaml",
        ),
        config=config,
    )

    assert expired.id not in store.entries
    assert admitted.tier == "retrieve"
    assert refused.tier == "quarantine"
    assert refused.payload["quarantine_reason"] == "quota_exceeded"
    assert sum(
        entry.kind == "convention" and entry.tier == "retrieve"
        for entry in store.iterate()
    ) == 256


def test_ttl_sets_expires_at_when_record_is_promoted() -> None:
    record = _record()
    before = time.time()

    entry = ingest_convention(
        MemoryStore(),
        record,
        config=_config(convention_approvals=[_approval(record)]),
    )
    after = time.time()

    expires_at = entry.payload["record"]["expires_at"]
    assert expires_at is not None
    assert before + 180 * 86400 <= expires_at <= after + 180 * 86400


def test_superseding_by_task_origin_is_quarantined_and_old_stays_active() -> None:
    store = MemoryStore()
    config = _config(convention_trusted_sources=["company_config"])
    old = ingest_convention(
        store,
        _record(
            version="1",
            origin="company_config",
            scope="company",
            provenance="/configs/company.yaml",
        ),
        config=config,
    )

    proposed = ingest_convention(
        store,
        _record(
            version="2",
            origin="task",
            scope="company",
            provenance="session:s-2",
            supersedes="1",
        ),
        config=config,
    )

    assert proposed.tier == "quarantine"
    assert proposed.payload["quarantine_reason"] == "supersession_not_authorized"
    assert store.entries[old.id].tier == "retrieve"


@pytest.mark.parametrize("verification", ["human_verified", "config_verified"])
def test_new_authority_verification_values_are_allowed(verification: str) -> None:
    assert verification_allowed(
        MemoryEntry(text="verified", kind="convention", verification=verification)
    )
