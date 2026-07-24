from __future__ import annotations

import json
import logging
import time

import pytest

from fabri.core.agent import run_agent
from fabri.core.llm import LLMResponse, ScriptedLLMBackend
from fabri.memory.compress import count_tokens
from fabri.memory.conventions import ConventionRecord, render_convention
from fabri.memory.schema import MemoryEntry
from fabri.orchestrator import retrieval
from fabri.orchestrator.convention_application import validate_branch_selection
from fabri.orchestrator.retrieval import (
    CONVENTION_APPLICATION_INSTRUCTION,
    RetrievalConfig,
    retrieve_context_with_meta,
)
from fabri.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit

BRANCH_A = {
    "evidence_state": "REPORT_ONLY",
    "response_mode": "DIRECT_REPLY",
    "detail_policy": "CUSTOMER_FACTS",
    "followup": "VERIFY_ACCOUNT",
}
BRANCH_B = {
    "evidence_state": "MITIGATED",
    "response_mode": "EXTERNAL_STATUS",
    "detail_policy": "CLASS_ONLY",
    "followup": "MONITOR_UPDATE",
}
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["response", *BRANCH_A],
    "properties": {
        "response": {"type": "string"},
        **{field: {"type": "string"} for field in BRANCH_A},
    },
}


class MemoryStore:
    def __init__(self, entries: list[MemoryEntry]) -> None:
        self.entries = entries

    def count(self) -> int:
        return len(self.entries)

    def query_by_vector(
        self,
        vector: list[float],
        top_k: int = 5,
        kind: str | None = None,
        tools_any: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        del vector
        entries = [
            entry
            for entry in self.entries
            if (kind is None or entry.kind == kind)
            and (tools_any is None or set(entry.tools) & set(tools_any))
        ]
        return [
            (entry, 0.9 - index * 0.01)
            for index, entry in enumerate(entries[:top_k])
        ]


def _record(*, expires_at: float | None = None) -> ConventionRecord:
    return ConventionRecord(
        scope="company",
        key="SHQ-E1",
        version="1",
        effect_class="response_mapping",
        conditions=[
            {
                "branch_id": "report_only",
                "condition_text": "Customer assertion lacks operational corroboration.",
            },
            {
                "branch_id": "mitigated",
                "condition_text": (
                    "Multi-customer impact was rolled back to baseline without "
                    "evidence of a permanent remedy."
                ),
            },
        ],
        branches=[
            {"branch_id": "report_only", "fields": BRANCH_A},
            {"branch_id": "mitigated", "fields": BRANCH_B},
        ],
        origin="company_config",
        provenance="companies/support-hq/company.toml",
        expires_at=expires_at,
        response_schema={
            field: {"type": "string"} for field in BRANCH_A
        },
    )


def _entry(*, expires_at: float | None = None) -> MemoryEntry:
    record = _record(expires_at=expires_at)
    return MemoryEntry(
        text=render_convention(record),
        kind="convention",
        scope="company",
        verification="config_verified",
        tier="retrieve",
        payload={"record": record.to_dict(), "status": "active"},
    )


def _selection(branch_id: str, fields: dict[str, str]) -> dict[str, object]:
    return {
        "response": "Customer-safe incident update.",
        "selected_branch_id": branch_id,
        "current_run_evidence": (
            "The incident workspace shows current multi-customer impact and rollback."
        ),
        **fields,
    }


@pytest.mark.parametrize(
    ("case", "branch_id", "fields"),
    [
        pytest.param("clear branch A", "report_only", BRANCH_A, id="clear_branch_a"),
        pytest.param("clear branch B", "mitigated", BRANCH_B, id="clear_branch_b"),
    ],
)
def test_clear_branch_selection_is_valid(
    case: str,
    branch_id: str,
    fields: dict[str, str],
) -> None:
    del case

    result = validate_branch_selection(
        _selection(branch_id, fields),
        [_entry(expires_at=time.time() + 3600)],
        config={"memory": {"convention_branch_selection_max_retries": 1}},
    )

    assert result.valid is True
    assert result.reason is None
    assert result.selected_branch_id == branch_id
    assert result.max_retries == 1


def test_blended_branch_fields_are_repaired_by_engine_copy() -> None:
    # A unique, evidenced selection is valid even when the model hand-copied
    # a value from the other branch — the caller applies selected_fields
    # verbatim, so blending can no longer reach the final output.
    blended = {**BRANCH_A, "response_mode": BRANCH_B["response_mode"]}

    result = validate_branch_selection(
        _selection("report_only", blended),
        [_entry(expires_at=time.time() + 3600)],
        config={"memory": {"convention_branch_selection_max_retries": 1}},
    )

    assert result.valid is True
    assert result.selected_branch_id == "report_only"
    assert result.selected_fields == BRANCH_A


def test_correct_mapping_without_selected_branch_id_is_invalid() -> None:
    output = _selection("mitigated", BRANCH_B)
    output.pop("selected_branch_id")

    result = validate_branch_selection(
        output,
        [_entry(expires_at=time.time() + 3600)],
        config={"memory": {"convention_branch_selection_max_retries": 1}},
    )

    assert result.valid is False
    assert result.reason == "selected_branch_id_missing"


def test_no_match_retries_once_then_returns_convention_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    invalid = _selection("no_matching_branch", BRANCH_B)
    backend = ScriptedLLMBackend(
        [
            LLMResponse(final_text=json.dumps(invalid)),
            LLMResponse(final_text=json.dumps(invalid)),
        ]
    )

    result = run_agent(
        "Prepare the SHQ-E1 customer update.",
        backend,
        ToolRegistry([]),
        MemoryStore([_entry(expires_at=time.time() + 3600)]),
        response_schema=RESPONSE_SCHEMA,
        response_retries=0,
        retrieval_config=RetrievalConfig(
            strategy="dense",
            importance_weight=0,
            convention_mining_enabled=True,
            convention_branch_selection_max_retries=1,
        ),
    )

    assert backend._i == 2
    assert result["success"] is True
    assert result["convention_application"] == "convention_not_applicable"
    # Fail-closed means the engine copies nothing — the model's schema-valid
    # answer is preserved untouched, never stripped (stripping turned wrong
    # answers into schema-invalid ones in live smoke r8).
    assert result["structured_output"] == invalid


def test_convention_mining_off_does_not_add_validation_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    output = {"response": "plain", **BRANCH_B}
    backend = ScriptedLLMBackend([LLMResponse(final_text=json.dumps(output))])

    result = run_agent(
        "Prepare a customer update.",
        backend,
        ToolRegistry([]),
        MemoryStore([_entry(expires_at=time.time() + 3600)]),
        response_schema=RESPONSE_SCHEMA,
        response_retries=0,
        retrieval_config=RetrievalConfig(
            strategy="dense",
            importance_weight=0,
            convention_mining_enabled=False,
        ),
    )

    assert backend._i == 1
    assert result["structured_output"] == output
    assert "convention_application" not in result


def test_no_retrieved_conventions_leave_structured_output_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    output = {"response": "plain", **BRANCH_B}
    backend = ScriptedLLMBackend([LLMResponse(final_text=json.dumps(output))])

    result = run_agent(
        "Prepare a customer update.",
        backend,
        ToolRegistry([]),
        MemoryStore([]),
        response_schema=RESPONSE_SCHEMA,
        response_retries=0,
        retrieval_config=RetrievalConfig(
            strategy="dense",
            importance_weight=0,
            convention_mining_enabled=True,
        ),
    )

    assert backend._i == 1
    assert result["structured_output"] == output
    assert "convention_application" not in result


def test_over_budget_convention_is_excluded_whole(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    entry = _entry(expires_at=time.time() + 3600)
    complete_render = entry.text + "\n" + CONVENTION_APPLICATION_INSTRUCTION
    caplog.set_level(logging.WARNING, logger="fabri")

    text, meta = retrieve_context_with_meta(
        MemoryStore([entry]),
        "SHQ-E1 update",
        retrieval_config=RetrievalConfig(
            strategy="dense",
            importance_weight=0,
            convention_mining_enabled=True,
            convention_max_tokens=count_tokens(complete_render) - 1,
        ),
    )

    assert text == ""
    assert meta["retrieved"] == 0
    assert "..." not in text
    assert "complete render exceeds convention_max_tokens" in caplog.text


def test_eligible_convention_renders_full_table_and_application_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    entry = _entry(expires_at=time.time() + 3600)

    text, meta = retrieve_context_with_meta(
        MemoryStore([entry]),
        "SHQ-E1 update",
        retrieval_config=RetrievalConfig(
            strategy="dense",
            importance_weight=0,
            convention_mining_enabled=True,
            convention_max_tokens=512,
        ),
    )

    assert BRANCH_A["evidence_state"] in text
    assert BRANCH_B["evidence_state"] in text
    assert CONVENTION_APPLICATION_INSTRUCTION in text
    assert "selected_branch_id" in text
    assert "current_run_evidence" in text
    assert "..." not in text
    assert meta["retrieved"] == 1
    assert len(meta["retrieved_conventions"]) == 1


def test_expired_convention_is_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])

    text, meta = retrieve_context_with_meta(
        MemoryStore([_entry(expires_at=time.time() - 1)]),
        "SHQ-E1 update",
        retrieval_config=RetrievalConfig(
            strategy="dense",
            importance_weight=0,
            convention_mining_enabled=True,
            convention_max_tokens=512,
        ),
    )

    assert text == ""
    assert meta["retrieved"] == 0
    assert "retrieved_conventions" not in meta
