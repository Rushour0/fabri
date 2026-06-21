"""B5 — host-emitted `discrepancy` events get mined into a tactical guideline.

Mirrors the test_pruning.py pattern: relies on a running Qdrant. The trace dir
is redirected via FABRI_HOME so a synthetic JSONL trace can be read back by
`process_trace` without touching the developer's real .fabri/ tree.
"""
import json
import os
import uuid
from pathlib import Path

from fabri.events import EventType, emit_discrepancy
from fabri.memory.store import QdrantMemoryStore
from fabri.orchestrator.pipeline import process_trace
from fabri.orchestrator.traces import trace_path

COLLECTION = f"test_disc_{uuid.uuid4().hex[:8]}"


class _NoopLLM:
    """process_trace only invokes the LLM when there's a `final` event or a
    tool failure; a discrepancy-only trace exercises neither code path, so this
    stub is sufficient."""

    def chat(self, *a, **kw):  # pragma: no cover - not reached
        raise AssertionError("LLM should not be called for a discrepancy-only trace")


def test_emit_discrepancy_writes_expected_event_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    sid = f"sess_{uuid.uuid4().hex[:8]}"
    emit_discrepancy(sid, "src/world/tiles.py", "claimed_not_landed")
    line = trace_path(sid).read_text().strip().splitlines()[-1]
    ev = json.loads(line)
    assert ev["type"] == EventType.DISCREPANCY.value
    assert ev["path"] == "src/world/tiles.py"
    assert ev["reason"] == "claimed_not_landed"


def test_process_trace_mines_discrepancy_into_guideline(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    sid = f"sess_{uuid.uuid4().hex[:8]}"
    emit_discrepancy(sid, "data/map.json", "claimed_not_landed")

    store = QdrantMemoryStore(collection=COLLECTION)
    before = store.count()
    entries = process_trace(sid, store, _NoopLLM())
    after = store.count()

    assert len(entries) == 1
    assert "re-read the file" in entries[0].text
    assert "data/map.json" in entries[0].text
    assert after == before + 1

    store.delete(entries[0].id)
