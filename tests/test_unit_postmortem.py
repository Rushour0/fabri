"""M1 — whole-run postmortem memory. The builder is a pure, deterministic
function (no LLM, no store), so most of this runs without any infra. One
store-backed test mirrors test_discrepancy.py (needs a running Qdrant) to prove
the opt-in flag actually writes one extra entry and dedups on recurrence."""
import uuid

from fabri.orchestrator.pipeline import build_postmortem_text


def _events(task="build the thing", outcome="success", failures=()):
    evs = [{"type": "start", "task": task, "ts": 0.0}]
    for name, err in failures:
        evs.append({"type": "tool_call", "name": name,
                    "result": {"ok": False, "error": err}})
    evs.append({"type": "tool_call", "name": "ok_tool", "result": {"ok": True}})
    evs.append({"type": "usage", "step_count": 4})
    evs.append({"type": outcome if outcome in ("final", "failed", "incomplete") else "final",
                "outcome": outcome})
    return evs


def test_postmortem_captures_task_outcome_and_counts():
    text = build_postmortem_text(_events(outcome="incomplete"))
    assert "build the thing" in text
    assert "outcome=incomplete" in text
    assert "steps=4" in text
    assert "tool_calls=1" in text and "(0 failed)" in text
    assert "Repeated failures: none." in text


def test_postmortem_groups_repeated_failures_by_tool_and_signature():
    evs = _events(failures=[
        ("fetch_url", "timeout after 30s"),
        ("fetch_url", "timeout after 30s\nstack..."),  # same first line -> same group
        ("write_file", "permission denied"),
    ])
    text = build_postmortem_text(evs)
    assert "fetch_url×2 [timeout after 30s]" in text
    assert "write_file×1 [permission denied]" in text
    assert "(3 failed)" in text


def test_postmortem_is_deterministic():
    evs = _events(failures=[("a", "boom")])
    assert build_postmortem_text(evs) == build_postmortem_text(evs)


def test_postmortem_truncates_long_task():
    text = build_postmortem_text(_events(task="x" * 500), )
    assert "…" in text
    # task slice capped at 140 chars
    assert "x" * 141 not in text


def test_postmortem_falls_back_to_step_started_count_without_usage():
    evs = [{"type": "start", "task": "t"},
           {"type": "step_started", "step": 0},
           {"type": "step_started", "step": 1},
           {"type": "final", "outcome": "success"}]
    assert "steps=2" in build_postmortem_text(evs)


# --- store-backed (needs Qdrant, like test_discrepancy) ---

from fabri.memory.store import QdrantMemoryStore  # noqa: E402
from fabri.orchestrator.pipeline import process_trace  # noqa: E402
from fabri.orchestrator.traces import log_event  # noqa: E402

COLLECTION = f"test_pm_{uuid.uuid4().hex[:8]}"


class _NoopLLM:
    def step(self, *a, **kw):  # pragma: no cover - postmortem needs no LLM
        raise AssertionError("postmortem path must not call the LLM")


def _write_trace(sid, task, outcome):
    log_event(sid, {"type": "start", "task": task})
    log_event(sid, {"type": "tool_call", "name": "boom_tool",
                    "result": {"ok": False, "error": "kaboom"}})
    log_event(sid, {"type": "usage", "step_count": 2})
    log_event(sid, {"type": outcome, "outcome": outcome})


def test_process_trace_records_postmortem_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    store = QdrantMemoryStore(collection=COLLECTION)

    # Off (default): the synthetic failure mines a tactical guideline but no
    # postmortem entry. The NoopLLM would fire on the failure synthesis, so use
    # a separate trace with no failures for the off-case count check.
    sid_off = f"s_{uuid.uuid4().hex[:8]}"
    log_event(sid_off, {"type": "start", "task": "no failures here"})
    log_event(sid_off, {"type": "final", "outcome": "success"})
    before = store.count()
    off = process_trace(sid_off, store, _NoopLLM(), record_postmortem=False)
    assert all(e.kind != "postmortem" for e in off)
    assert store.count() == before  # nothing written

    # On: exactly one postmortem entry appears.
    on = process_trace(sid_off, store, _NoopLLM(), record_postmortem=True)
    pm = [e for e in on if e.kind == "postmortem"]
    assert len(pm) == 1
    assert "no failures here" in pm[0].text
    assert pm[0].hit_count == 1

    # Re-running the same trace dedups (recurrence), not duplicates.
    again = process_trace(sid_off, store, _NoopLLM(), record_postmortem=True)
    pm2 = [e for e in again if e.kind == "postmortem"]
    assert len(pm2) == 1
    assert pm2[0].id == pm[0].id
    assert pm2[0].hit_count == 2

    store.delete(pm[0].id)
