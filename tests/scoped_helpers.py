"""Shared fixtures for the scoped-memory tests: one base store per backend."""
from __future__ import annotations

import uuid

import pytest

QDRANT_URL = "http://localhost:6333"


def _qdrant_up() -> bool:
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=QDRANT_URL, timeout=2).get_collections()
        return True
    except Exception:
        return False


def backend_params():
    params = ["sqlite"]
    params.append(
        pytest.param(
            "qdrant",
            marks=pytest.mark.skipif(not _qdrant_up(), reason="Qdrant not running on localhost:6333"),
        )
    )
    return params


def make_base(backend: str, tmp_path):
    """A base store whose siblings are the scoped collections. The qdrant
    base collection name is unique per call so runs never collide."""
    if backend == "sqlite":
        from fabri.memory.embedded_store import SqliteMemoryStore

        return SqliteMemoryStore(path=tmp_path / "base.db", collection="base")
    from fabri.memory.store import QdrantMemoryStore

    return QdrantMemoryStore(url=QDRANT_URL, collection=f"test_base_{uuid.uuid4().hex[:8]}")
