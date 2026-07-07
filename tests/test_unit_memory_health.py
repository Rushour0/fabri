"""M6: `## memory health` section in `fabri report`.

Covers compute_memory_health over a real (sqlite) store, the render integration
across all three formats, JSON back-compat (avg_reuse_rate stays top-level), and
the critical offline-safety property: `fabri report` must still render when no
memory store is reachable. See docs/design/memory-observability-plan.md (unit E).
"""
import argparse
import json

import pytest

from fabri.reports import (
    AggregateReport,
    aggregate,
    compute_memory_health,
    render_html,
    render_json,
    render_markdown,
)


# --- compute_memory_health over a real store -------------------------------

def _sqlite_store(tmp_path):
    pytest.importorskip("sqlite_vec")
    from fabri.memory.embedded_store import SqliteMemoryStore

    return SqliteMemoryStore(path=tmp_path / "mh.db", collection="mh")


def _entry(text, kind="tactical", created_at=None):
    from fabri.memory.schema import MemoryEntry

    e = MemoryEntry(text=text, kind=kind)
    if created_at is not None:
        e.created_at = created_at
    return e


def test_compute_memory_health_basic(tmp_path):
    import time

    store = _sqlite_store(tmp_path)
    now = time.time()
    store.upsert(_entry("tactical one", "tactical", now - 10 * 86400))
    store.upsert(_entry("tactical two", "tactical", now - 2 * 86400))
    store.upsert(_entry("strategic one", "strategic", now - 30 * 86400))
    store.upsert(_entry("success one", "success_pattern", now - 1 * 86400))

    mh = compute_memory_health(store)
    assert mh["total_guidelines"] == 4
    assert mh["strategic_share"] == round(1 / 4, 4)
    assert mh["median_age_days"] is not None and mh["median_age_days"] > 0
    assert mh["by_kind"]["tactical"] == 2
    assert mh["by_kind"]["strategic"] == 1
    assert mh["by_kind"]["success_pattern"] == 1


def test_compute_memory_health_empty_store(tmp_path):
    store = _sqlite_store(tmp_path)
    mh = compute_memory_health(store)
    assert mh["total_guidelines"] == 0
    assert mh["strategic_share"] is None
    assert mh["median_age_days"] is None
    assert mh["by_kind"] == {}


# --- render integration (no store needed) ----------------------------------

_HEALTH = {
    "total_guidelines": 12,
    "strategic_share": 0.25,
    "median_age_days": 7.5,
    "by_kind": {"tactical": 9, "strategic": 3},
}


def _report_with_health():
    report = aggregate([])
    report.memory_health = _HEALTH
    report.avg_reuse_rate = 0.42
    return report


def test_markdown_includes_memory_health():
    out = render_markdown(_report_with_health())
    assert "## memory health" in out
    assert "guidelines in store" in out
    assert "12" in out


def test_html_includes_memory_health():
    out = render_html(_report_with_health())
    assert "memory health" in out


def test_json_keeps_avg_reuse_rate_toplevel_and_adds_health():
    payload = json.loads(render_json(_report_with_health()))
    # Back-compat: avg_reuse_rate stays at the top level.
    assert payload["avg_reuse_rate"] == 0.42
    assert payload["memory_health"]["total_guidelines"] == 12


def test_render_omits_section_when_no_health():
    # The default (no store reachable) — report renders with no memory section.
    report = aggregate([])
    assert report.memory_health is None
    md = render_markdown(report)
    assert "## memory health" not in md
    assert json.loads(render_json(report))["memory_health"] is None


# --- offline safety: report survives an unreachable store ------------------

def test_cmd_report_offline_safe_when_store_raises(tmp_path, monkeypatch, capsys):
    """The E2 fix: build_memory_store blowing up must NOT kill the report."""
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))  # empty traces dir

    def _boom(_cfg):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr("fabri.cli.build_memory_store", _boom)

    from fabri.cli import cmd_report

    args = argparse.Namespace(since=None, limit=None, config=None, format="json", output=None)
    cmd_report(args)  # must not raise

    payload = json.loads(capsys.readouterr().out)
    assert payload["memory_health"] is None  # gracefully skipped
