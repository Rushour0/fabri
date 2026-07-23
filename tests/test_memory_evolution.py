from __future__ import annotations

from pathlib import Path

import pytest

from fabri.core.agent import run_agent
from fabri.core.llm import LLMResponse, LLMUsage, ScriptedLLMBackend
from fabri.memory.schema import MemoryEntry
from fabri.memory.verification import apply_session_verification
from fabri.benchmarks.company_evolution import (
    apply_training_verification,
    create_company_snapshot,
    evaluate_promotion,
    load_evaluation_variants,
    promote_company_snapshot,
    restore_company_snapshot,
)
from fabri.benchmarks.company_memory_study import MemoryCase, _smoke_gate
from fabri.orchestrator import pipeline, retrieval
from fabri.orchestrator.pipeline import MiningReport, process_trace
from fabri.orchestrator.retrieval import RetrievalConfig, retrieve_context_with_meta
from fabri.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


class MemoryStore:
    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self.collection = "memory-evolution-test"
        self.entries = {entry.id: entry for entry in entries or []}

    def find_by_dedup_key(
        self, dedup_key: str, kind: str | None = None
    ) -> tuple[MemoryEntry, float] | None:
        for entry in self.entries.values():
            if entry.dedup_key == dedup_key and (kind is None or entry.kind == kind):
                return entry, 1.0
        return None

    def find_similar(
        self, text: str, threshold: float = 0.85, kind: str | None = None
    ) -> tuple[MemoryEntry, float] | None:
        del text, threshold, kind
        return None

    def upsert(self, entry: MemoryEntry) -> str:
        self.entries[entry.id] = entry
        return entry.id

    def delete(self, point_id: str) -> None:
        self.entries.pop(point_id, None)

    def iterate(self, kind: str | None = None, limit: int | None = None) -> list[MemoryEntry]:
        entries = [entry for entry in self.entries.values() if kind is None or entry.kind == kind]
        return entries[:limit] if limit is not None else entries

    def count(self) -> int:
        return len(self.entries)

    def query_by_vector(
        self, vector: list[float], top_k: int = 5,
        kind: str | None = None, tools_any: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        del vector
        entries = [
            entry for entry in self.entries.values()
            if (kind is None or entry.kind == kind)
            and (tools_any is None or set(entry.tools) & set(tools_any))
        ]
        return [(entry, 0.9 - index * 0.01) for index, entry in enumerate(entries[:top_k])]


class NoOpLLM:
    def step(self, system: str, messages: list[dict]) -> object:
        raise AssertionError("deterministic mining must not call the LLM")


def test_mining_report_records_provenance_and_verified_deterministic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    monkeypatch.setattr(pipeline, "embeddings_available", lambda: True)
    store = MemoryStore()
    reports: list[MiningReport] = []
    events = [
        {"type": "start", "task": "repair checkout"},
        {
            "type": "tool_call", "name": "read_file", "args": {"path": "src/a.py"},
            "result": {"ok": False, "error": "missing"},
        },
        {"type": "failed", "outcome": "failed"},
    ]

    entries = process_trace(
        "session-one", store, NoOpLLM(), events=events, synthesize=False,
        producer_agent_id="triager", on_report=reports.append,
    )

    assert len(entries) == 1
    assert entries[0].producer_agent_id == "triager"
    assert entries[0].source_session_ids == ["session-one"]
    assert entries[0].source_event_ids
    assert entries[0].verification == "tool_verified"
    assert reports[0].candidates_produced == 1
    assert reports[0].inserted == 1
    assert "success_missing_final" in reports[0].skip_reasons


def test_verified_retrieval_excludes_unverified_and_contradicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    verified = MemoryEntry(
        text="verified recovery", kind="tactical", verification="tool_verified"
    )
    unverified = MemoryEntry(text="candidate", kind="tactical")
    contradicted = MemoryEntry(
        text="bad lesson", kind="success_pattern", verification="contradicted"
    )
    store = MemoryStore([unverified, verified, contradicted])

    text, meta = retrieve_context_with_meta(
        store, "repair checkout", retrieval_config=RetrievalConfig(
            strategy="dense", importance_weight=0, verification="verified"
        )
    )

    assert "verified recovery" in text
    assert "candidate" not in text
    assert "bad lesson" not in text
    assert meta["retrieved"] == 1


def test_verified_retrieval_is_not_starved_by_higher_unverified_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    candidates = [
        MemoryEntry(text=f"candidate {index}", kind="tactical") for index in range(12)
    ]
    verified = MemoryEntry(
        text="verified tail lesson", kind="tactical", verification="rubric_verified"
    )
    store = MemoryStore([*candidates, verified])

    text, meta = retrieve_context_with_meta(
        store, "repair checkout", top_k=5,
        retrieval_config=RetrievalConfig(
            strategy="dense", importance_weight=0, verification="verified"
        ),
    )

    assert "verified tail lesson" in text
    assert meta["retrieved"] == 1


def test_first_cross_session_reuse_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    monkeypatch.setattr(retrieval, "_emit_retrieval_event", lambda *_args: None)
    entry = MemoryEntry(
        text="lesson mined in session A",
        kind="tactical",
        session_ids=["session-a"],
        hit_count=1,
    )

    _, meta = retrieve_context_with_meta(
        MemoryStore([entry]),
        "reuse lesson",
        session_id="session-b",
        retrieval_config=RetrievalConfig(strategy="dense", importance_weight=0),
    )

    assert meta["from_prior_sessions"] == 1


def test_same_session_retrieval_does_not_count_as_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    monkeypatch.setattr(retrieval, "_emit_retrieval_event", lambda *_args: None)
    entry = MemoryEntry(
        text="lesson mined in current session",
        kind="tactical",
        session_ids=["session-a"],
        hit_count=2,
    )

    _, meta = retrieve_context_with_meta(
        MemoryStore([entry]),
        "reuse lesson",
        session_id="session-a",
        retrieval_config=RetrievalConfig(strategy="dense", importance_weight=0),
    )

    assert meta["from_prior_sessions"] == 0


def test_reuse_metric_falls_back_to_hit_count_without_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda text: [1.0])
    entry = MemoryEntry(
        text="legacy recurring lesson",
        kind="tactical",
        hit_count=2,
    )

    _, meta = retrieve_context_with_meta(
        MemoryStore([entry]),
        "reuse lesson",
        retrieval_config=RetrievalConfig(strategy="dense", importance_weight=0),
    )

    assert meta["from_prior_sessions"] == 1


def test_apply_session_verification_updates_only_matching_success_patterns() -> None:
    matching = MemoryEntry(
        text="worked", kind="success_pattern", source_session_ids=["s1"]
    )
    other_session = MemoryEntry(
        text="also worked", kind="success_pattern", source_session_ids=["s2"]
    )
    failure_hint = MemoryEntry(
        text="avoid this", kind="tactical", source_session_ids=["s1"]
    )
    store = MemoryStore([matching, other_session, failure_hint])

    updated = apply_session_verification(store, "s1", "rubric_verified")

    assert updated == [matching.id]
    assert matching.verification == "rubric_verified"
    assert other_session.verification == "unverified"
    assert failure_hint.verification == "unverified"


def test_legacy_payload_populates_canonical_provenance() -> None:
    entry = MemoryEntry.from_payload({
        "text": "legacy", "kind": "tactical", "session_ids": ["old"],
        "agent_id": "worker",
    })

    assert entry.source_session_ids == ["old"]
    assert entry.producer_agent_id == "worker"
    assert entry.verification == "unverified"


def _compiled_company(root: Path, label: str) -> Path:
    destination = root / label
    company = destination / "support-hq"
    configs = {
        company / "ceo.yaml": ("ceo", company / ".fabri" / "root.db"),
        company / "agencies" / "crew" / "agent.yaml": (
            "specialist", company / ".fabri" / "crew.db",
        ),
    }
    for config, (agent, database) in configs.items():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            f"agent: {{name: {agent}}}\nmemory:\n  backend: sqlite\n"
            f"  sqlite_path: {database}\n",
            encoding="utf-8",
        )
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_bytes(f"{label}:{agent}".encode())
    return destination


def _verification_case() -> MemoryCase:
    return MemoryCase(
        case_id="verif",
        company_source=Path("company.toml"),
        company_name="support-hq",
        namespace="support_hq",
        root_id="ceo",
        training_prompt="train",
        holdout_prompt="hold",
        required_terms=(("rollback",),),
        forbidden_terms=("blame",),
        required_delegations=(),
        retrieval_expectations={},
        replicas=1,
        conditions=("memory",),
    )


def _verification_stores() -> tuple[dict[str, MemoryStore], MemoryEntry, MemoryEntry]:
    success = MemoryEntry(
        text="checkout rollback worked",
        kind="success_pattern",
        source_session_ids=["s1"],
        producer_agent_id="specialist",
    )
    tool_failure = MemoryEntry(
        text="avoid stale cache",
        kind="tactical",
        source_session_ids=["s1"],
        verification="tool_verified",
        producer_agent_id="specialist",
    )
    stores = {
        ".fabri/root.db": MemoryStore([success, tool_failure]),
        ".fabri/crew.db": MemoryStore([]),
    }
    return stores, success, tool_failure


def test_training_verification_promotes_success_patterns_when_run_succeeded(
    tmp_path: Path,
) -> None:
    compiled = _compiled_company(tmp_path, "trained")
    stores, success, tool_failure = _verification_stores()

    result = apply_training_verification(
        compiled,
        _verification_case(),
        training_succeeded=True,
        store_factory=lambda _path, collection: stores[collection],
    )

    assert result["training_succeeded"] is True
    assert result["rubric_verified_entry_ids"] == [success.id]
    assert success.verification == "rubric_verified"
    # A deterministic tool-failure lesson stays tool_verified, untouched.
    assert tool_failure.verification == "tool_verified"


def test_training_verification_leaves_unverified_when_run_failed(
    tmp_path: Path,
) -> None:
    compiled = _compiled_company(tmp_path, "trained")
    stores, success, _tool_failure = _verification_stores()

    result = apply_training_verification(
        compiled,
        _verification_case(),
        training_succeeded=False,
        store_factory=lambda _path, collection: stores[collection],
    )

    assert result["training_succeeded"] is False
    assert result["rubric_verified_entry_ids"] == []
    # A failed/truncated training run must not fabricate trust in its lessons.
    assert success.verification == "unverified"


def test_company_snapshot_restores_all_dbs_and_promotes_atomically(tmp_path: Path) -> None:
    trained = _compiled_company(tmp_path, "trained")
    fresh = _compiled_company(tmp_path, "fresh")

    snapshot = create_company_snapshot(
        trained, "support-hq", tmp_path / "snapshots", "generation-001"
    )
    restored = restore_company_snapshot(snapshot, fresh, "support-hq")
    pointer = promote_company_snapshot(snapshot, tmp_path / "snapshots" / "current.json")

    assert restored == (".fabri/crew.db", ".fabri/root.db")
    assert (fresh / "support-hq" / ".fabri" / "root.db").read_bytes() == b"trained:ceo"
    assert (fresh / "support-hq" / ".fabri" / "crew.db").read_bytes() == b"trained:specialist"
    assert pointer["generation_id"] == "generation-001"


def test_promotion_gate_accepts_quality_tie_with_material_cost_gain() -> None:
    pairs = [
        {
            "variant_id": f"variant-{index % 3}",
            "incumbent": {
                "complete": True, "rubric_passed": True, "forbidden_hits": [],
                "total_cost_usd": 0.10, "total_retries": 2,
            },
            "candidate": {
                "complete": True, "rubric_passed": True, "forbidden_hits": [],
                "total_cost_usd": 0.08, "total_retries": 1,
                "verified_specialist_entry_ids": ["lesson"] if index % 3 < 2 else [],
            },
        }
        for index in range(6)
    ]

    decision = evaluate_promotion(pairs)

    assert decision.promote is True
    assert decision.cost_ratio == pytest.approx(0.8)
    assert decision.retry_reduction == pytest.approx(0.5)


def test_promotion_gate_rejects_one_quality_regression() -> None:
    pairs = [
        {
            "variant_id": f"variant-{index % 3}",
            "incumbent": {
                "complete": True, "rubric_passed": True, "forbidden_hits": [],
                "total_cost_usd": 0.10, "total_retries": 1,
            },
            "candidate": {
                "complete": True, "rubric_passed": index != 0, "forbidden_hits": [],
                "total_cost_usd": 0.05, "total_retries": 0,
                "verified_specialist_entry_ids": ["lesson"],
            },
        }
        for index in range(6)
    ]

    decision = evaluate_promotion(pairs)

    assert decision.promote is False
    assert "quality_regression" in decision.reasons


def test_supply_smoke_gate_requires_specialist_transport_and_retrieval() -> None:
    runs: list[dict[str, object]] = []
    for replica in (1, 2):
        runs.append({
            "replica": replica,
            "condition": "memory",
            "guidelines_retrieved": 1,
            "funnel": {
                "supply": {"mining_reports": [{
                    "producer_agent_id": "specialist", "entry_ids": [f"lesson-{replica}"],
                }]},
                "transport": {"intact": True, "dbs": [{"present": True}]},
                "retrieval": {"transported_entry_ids_retrieved": [f"lesson-{replica}"]},
            },
        })
        runs.append({
            "replica": replica,
            "condition": "control",
            "guidelines_retrieved": 0,
            "funnel": {
                "transport": {"intact": True, "dbs": [{"present": False}]},
            },
        })

    assert _smoke_gate(runs, "ceo") == {"go": True, "failures": []}


def test_evolution_dataset_requires_three_frozen_variants(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text(
        "cases:\n"
        "  - id: support\n"
        "    evolution:\n"
        "      variants:\n"
        "        - {id: anchor, prompt: one, expected: {required: [one], forbidden: []}}\n"
        "        - {id: variant-a, prompt: two, expected: {required: [two], forbidden: []}}\n"
        "        - {id: variant-b, prompt: three, expected: {required: [three], forbidden: []}}\n",
        encoding="utf-8",
    )

    variants = load_evaluation_variants(dataset, "support")

    assert [variant.variant_id for variant in variants] == ["anchor", "variant-a", "variant-b"]


def test_run_usage_aggregates_provider_and_token_retries() -> None:
    backend = ScriptedLLMBackend([LLMResponse(
        final_text="done",
        usage=LLMUsage(provider_transient_retries=2, max_token_retries=1),
    )])

    result = run_agent("finish", backend, ToolRegistry([]), MemoryStore())

    assert result["usage"]["provider_transient_retries"] == 2
    assert result["usage"]["max_token_retries"] == 1
