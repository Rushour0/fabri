"""Offline coverage for the lean install's optional embeddings behavior."""

import subprocess
import sys

from fabri.memory.embeddings import EMBEDDING_DIM
from fabri.orchestrator import pipeline, retrieval


class _UnusedStore:
    def count(self) -> int:
        raise AssertionError("retrieval must not touch the store without embeddings")


def test_retrieval_is_empty_when_embeddings_are_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("fabri.memory.embeddings.embeddings_available", lambda: False)
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: False)

    text, meta = retrieval.retrieve_context_with_meta(_UnusedStore(), "do work")

    assert text == ""
    assert meta == {"retrieved": 0, "from_prior_sessions": 0, "strategic": 0}


def test_memory_learning_is_a_noop_when_embeddings_are_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("fabri.memory.embeddings.embeddings_available", lambda: False)
    monkeypatch.setattr(pipeline, "embeddings_available", lambda: False)

    entries = pipeline.process_trace("session", _UnusedStore(), llm=None, events=[])

    assert entries == []


def test_embedding_constant_and_imports_remain_lightweight() -> None:
    assert EMBEDDING_DIM == 384
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import fabri.memory.embeddings, fabri.orchestrator.retrieval; "
            "assert 'sentence_transformers' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
