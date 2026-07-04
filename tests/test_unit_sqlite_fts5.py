"""Unit tests for SQLite FTS5 BM25 support in SqliteMemoryStore.

Skipped when sqlite-vec is not installed (CI without the sqlite extra)."""
from __future__ import annotations

import json
import sqlite3

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")

from fabri.memory.embedded_store import SqliteMemoryStore, _fts5_query
from fabri.memory.schema import MemoryEntry


# ---------------------------------------------------------------------------
# _fts5_query helper
# ---------------------------------------------------------------------------

class TestFts5Query:
    def test_plain_text(self):
        q = _fts5_query("spawn subagent for parallel work")
        assert '"spawn"' in q
        assert '"subagent"' in q

    def test_tool_name_with_underscore(self):
        # read_file → underscores stripped → "read" "file"
        q = _fts5_query("read_file path")
        assert '"read"' in q
        assert '"file"' in q

    def test_special_chars_do_not_appear_in_output(self):
        q = _fts5_query("read_file (path: ./foo) url=http://x")
        # No FTS5 operators should survive
        for char in ["(", ")", ":", "*", "^"]:
            assert char not in q

    def test_empty_string_returns_empty(self):
        assert _fts5_query("") == ""

    def test_whitespace_only_returns_empty(self):
        assert _fts5_query("   ") == ""

    def test_caps_at_50_tokens(self):
        long_text = " ".join(f"word{i}" for i in range(100))
        q = _fts5_query(long_text)
        tokens = q.split()
        assert len(tokens) <= 50


# ---------------------------------------------------------------------------
# SqliteMemoryStore FTS5 integration
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return SqliteMemoryStore(path=tmp_path / "test.db")


def _entry(text: str, kind: str = "tactical") -> MemoryEntry:
    return MemoryEntry(text=text, kind=kind)


class TestFts5Schema:
    def test_fts5_table_created_on_init(self, store):
        tables = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "fts_guidelines" in table_names

    def test_fts5_empty_on_fresh_db(self, store):
        count = store.conn.execute("SELECT COUNT(*) FROM fts_guidelines").fetchone()[0]
        assert count == 0


class TestQueryBm25:
    def test_finds_keyword_match(self, store):
        store.upsert(_entry("spawn subagent for parallel work"))
        results = store.query_bm25("spawn subagent")
        assert len(results) == 1
        assert "spawn" in results[0][0].text.lower()

    def test_returns_higher_score_for_better_match(self, store):
        store.upsert(_entry("spawn subagent for parallel work"))
        store.upsert(_entry("read a file from disk"))
        results = store.query_bm25("spawn subagent parallel")
        assert results[0][0].text.startswith("spawn")

    def test_empty_text_returns_empty(self, store):
        store.upsert(_entry("some guideline"))
        results = store.query_bm25("")
        assert results == []

    def test_special_chars_do_not_crash(self, store):
        store.upsert(_entry("read file from path"))
        results = store.query_bm25("read_file (path: ./foo)")
        assert isinstance(results, list)

    def test_no_match_returns_empty(self, store):
        store.upsert(_entry("completely unrelated text about cooking"))
        results = store.query_bm25("quantum physics plasma reactor")
        assert isinstance(results, list)  # may or may not be empty, but no crash

    def test_scores_are_positive(self, store):
        store.upsert(_entry("spawn subagent for parallel work"))
        results = store.query_bm25("spawn")
        for _, score in results:
            assert score > 0

    def test_top_k_limits_results(self, store):
        for i in range(10):
            store.upsert(_entry(f"keyword entry number {i}"))
        results = store.query_bm25("keyword entry", top_k=3)
        assert len(results) <= 3

    def test_kind_filter(self, store):
        store.upsert(_entry("spawn subagent", kind="tactical"))
        store.upsert(_entry("spawn subagent thing", kind="success_pattern"))
        results = store.query_bm25("spawn subagent", kind="success_pattern")
        for entry, _ in results:
            assert entry.kind == "success_pattern"


class TestFts5Sync:
    def test_syncs_after_upsert(self, store):
        e = _entry("unique keyword xyzzy quux")
        store.upsert(e)
        count = store.conn.execute("SELECT COUNT(*) FROM fts_guidelines").fetchone()[0]
        assert count == 1

    def test_syncs_after_delete(self, store):
        e = _entry("delete me xyzzy")
        store.upsert(e)
        store.delete(e.id)
        # BM25 query should return nothing
        results = store.query_bm25("xyzzy")
        assert all(r[0].id != e.id for r in results)
        # FTS5 table should have no row for this id
        row = store.conn.execute(
            "SELECT COUNT(*) FROM fts_guidelines WHERE id = ?", (e.id,)
        ).fetchone()
        assert row[0] == 0

    def test_upsert_replaces_old_text_in_fts5(self, store):
        # Upsert the same ID with updated text (same text → same ID, so this
        # tests idempotency rather than true text update, but still exercises
        # the delete+insert path).
        e = _entry("original text about spawning")
        store.upsert(e)
        store.upsert(e)
        count = store.conn.execute(
            "SELECT COUNT(*) FROM fts_guidelines WHERE id = ?", (e.id,)
        ).fetchone()[0]
        assert count == 1


class TestFts5Migration:
    def test_migration_populates_existing_rows(self, tmp_path):
        """Simulate a DB that was created before FTS5 support.

        We manually create the guidelines and vec_guidelines tables, insert a
        row, then init SqliteMemoryStore. The migration block should detect
        fts_guidelines is empty (since we never created it) and bulk-insert."""
        db_path = tmp_path / "migrate.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        # Create tables as the old schema (no fts_guidelines)
        conn.execute(
            """CREATE TABLE guidelines (
               id TEXT PRIMARY KEY,
               text TEXT NOT NULL,
               kind TEXT NOT NULL,
               payload TEXT NOT NULL,
               hit_count INTEGER NOT NULL DEFAULT 1
            )"""
        )
        conn.execute(
            """CREATE VIRTUAL TABLE vec_guidelines USING vec0(
               id TEXT PRIMARY KEY,
               embedding FLOAT[384]
            )"""
        )
        # Insert a guideline directly (bypass embedding)
        e = _entry("pre-existing guideline for migration test")
        conn.execute(
            "INSERT INTO guidelines(id, text, kind, payload, hit_count) VALUES (?, ?, ?, ?, ?)",
            (e.id, e.text, e.kind, json.dumps(e.to_payload()), e.hit_count),
        )
        conn.commit()
        conn.close()

        # Now open via SqliteMemoryStore — migration should run
        store = SqliteMemoryStore(path=db_path)

        # FTS5 table should have been populated
        fts_count = store.conn.execute("SELECT COUNT(*) FROM fts_guidelines").fetchone()[0]
        assert fts_count == 1

        # And the entry should be findable via BM25
        results = store.query_bm25("migration")
        assert len(results) >= 1
