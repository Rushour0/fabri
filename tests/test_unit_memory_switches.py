"""Unit coverage for the explicit memory mining and retrieval switches."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fabri.benchmarks.company_memory_study import apply_retrieval_overrides
from fabri.config import DEFAULT_CONFIG
from fabri.orchestrator import retrieval
from fabri.orchestrator.retrieval import RetrievalConfig, _retrieve_inner, retrieve_context


pytestmark = pytest.mark.unit


class _QueryFailingStore:
    def __init__(self) -> None:
        self.query_calls = 0

    def query_by_vector(self, *args: object, **kwargs: object) -> list[object]:
        self.query_calls += 1
        raise AssertionError("disabled retrieval must not query the store")


class _EmptyStore:
    def __init__(self) -> None:
        self.query_calls = 0

    def count(self) -> int:
        return 1

    def query_by_vector(
        self, *args: object, **kwargs: object
    ) -> list[tuple[object, float]]:
        self.query_calls += 1
        return []


def test_retrieval_disabled_returns_empty_context_without_embedding_or_store_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RetrievalConfig.from_mem_cfg({"retrieval_enabled": False})
    store = _QueryFailingStore()
    monkeypatch.setattr(retrieval, "embed", lambda _task: (_ for _ in ()).throw(AssertionError()))

    assert config.retrieval_enabled is False
    assert _retrieve_inner(store, "repair checkout", retrieval_config=config) == ("", [])
    assert retrieve_context(store, "repair checkout", retrieval_config=config) == ""
    assert store.query_calls == 0


def test_memory_switches_default_to_enabled_and_keep_the_default_retrieval_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_config = RetrievalConfig()
    absent_config = RetrievalConfig.from_mem_cfg({})
    assert DEFAULT_CONFIG["memory"]["mining_enabled"] is True
    assert DEFAULT_CONFIG["memory"]["retrieval_enabled"] is True
    assert default_config.retrieval_enabled is True
    assert absent_config == default_config

    monkeypatch.setattr(retrieval, "embeddings_available", lambda: True)
    monkeypatch.setattr(retrieval, "embed", lambda _task: [0.0])
    default_store = _EmptyStore()
    absent_store = _EmptyStore()
    assert _retrieve_inner(default_store, "repair checkout", retrieval_config=default_config) == ("", [])
    assert _retrieve_inner(absent_store, "repair checkout", retrieval_config=absent_config) == ("", [])
    assert default_store.query_calls == absent_store.query_calls == 1


def test_control_override_disables_mining_and_retrieval_for_every_compiled_node(
    tmp_path: Path,
) -> None:
    company_root = tmp_path / "compiled" / "support-hq"
    paths = [company_root / "ceo.yaml", company_root / "agencies" / "crew.yaml"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("agent: {name: worker}\nmemory: {backend: sqlite}\n", encoding="utf-8")

    changed = apply_retrieval_overrides(
        tmp_path / "compiled",
        "support-hq",
        top_k=None,
        strategy=None,
        mining_enabled=False,
        retrieval_enabled=False,
    )

    assert changed == sorted(str(path) for path in paths)
    for path in paths:
        memory = yaml.safe_load(path.read_text(encoding="utf-8"))["memory"]
        assert memory["mining_enabled"] is False
        assert memory["retrieval_enabled"] is False
