"""G16: sqlite-vec embedded memory store — same surface as QdrantMemoryStore,
no docker required.

Trade-off: sqlite-vec is single-process + file-backed. Use Qdrant in production
when multiple processes share a memory store (and for richer payload filters
at scale). sqlite-vec is the right call for demos, dev loops, CI, and
small/embedded deployments.

Config:
    memory:
      backend: sqlite                          # or "qdrant"
      path: .fabri/memory.db                   # sqlite-only
      qdrant_url: http://localhost:6333        # qdrant-only

The two backends are interchangeable from the agent loop's perspective —
`build_memory_store(mem_cfg)` (runtime.py) picks one.
"""
from __future__ import annotations

import json
import re as _re
import sqlite3
import struct
from pathlib import Path

from fabri.memory.embeddings import EMBEDDING_DIM, embed
from fabri.memory.schema import EMBEDDING_MODEL_VERSION, MemoryEntry

try:
    import sqlite_vec  # type: ignore[import-not-found]
    _HAS_SQLITE_VEC = True
except ImportError:
    _HAS_SQLITE_VEC = False


_INSTALL_HINT = (
    "memory.backend=sqlite requires the `sqlite-vec` package; install it with\n"
    "    pip install 'fabri[sqlite]'  (or)  pip install sqlite-vec"
)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _fts5_query(text: str) -> str:
    """Convert raw text to an FTS5 MATCH expression.

    Splits on any non-alphanumeric char — INCLUDING underscore, so a tool name
    like `read_file` contributes its parts (`read`, `file`) and matches
    guidelines that mention either — wraps each token in double quotes (so FTS5
    operator chars like *, ", (, ), ^, : can't cause a syntax error), and joins
    them with OR.

    The OR is load-bearing. FTS5 treats whitespace between terms as an implicit
    AND, so the old space-join required a guideline to contain EVERY query word
    — which for a natural-language task is almost never true, silently making
    BM25 (and therefore `sparse`/`hybrid` retrieval) a no-op that returned [].
    OR matches a guideline sharing ANY term; BM25's IDF weighting then ranks
    common words (the, for) near-zero so precision holds. Caps at 50 terms to
    bound the expression size. Returns "" when no tokens survive (caller skips
    the BM25 query entirely). Verified by the offline retrieval eval: fixing
    this lifts hybrid recall@5 from 0.79 (== dense, dead) to 0.94."""
    words = _re.sub(r"[\W_]+", " ", text).split()
    return " OR ".join(f'"{w}"' for w in words[:50]) if words else ""


class SqliteMemoryStore:
    """Drop-in alternative to QdrantMemoryStore using sqlite-vec for ANN.

    Schema:
      guidelines(id TEXT PK, text, kind, payload JSON, hit_count INT)
      vec_guidelines (vec0 virtual table) keyed by rowid -> (id) via guidelines.

    We store entries' UUID as the TEXT primary key and join through the rowid
    column for vec0. Distance is cosine (matches the Qdrant store config so
    similarity_threshold thresholds carry across backends without retuning).
    """

    def __init__(
        self,
        path: str | Path = ".fabri/memory.db",
        collection: str = "fabri",
    ):
        if not _HAS_SQLITE_VEC:
            raise RuntimeError(_INSTALL_HINT)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # `collection` is exposed for parity with QdrantMemoryStore — used by
        # memory/pruning.py to derive its per-collection ingest lock filename.
        # For sqlite the underlying db file IS the collection; this attribute
        # just labels it so multiple sqlite-backed agents in the same process
        # don't share an ingest lock when they share a Python interpreter.
        self.collection = collection
        self.conn = sqlite3.connect(str(path))
        # Enable sqlite-vec extension on this connection.
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS guidelines (
               id TEXT PRIMARY KEY,
               text TEXT NOT NULL,
               kind TEXT NOT NULL,
               payload TEXT NOT NULL,
               hit_count INTEGER NOT NULL DEFAULT 1
           )"""
        )
        cur.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS vec_guidelines USING vec0(
                id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            )"""
        )
        # FTS5 is a Python built-in — no extra install needed. Porter tokenizer
        # improves BM25 recall by stemming (run/running/runs → same stem).
        # id is UNINDEXED so it rides alongside each row for JOINs without
        # inflating the BM25 signal.
        cur.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS fts_guidelines USING fts5(
               id UNINDEXED,
               text,
               tokenize='porter ascii'
            )"""
        )
        # One-time migration for DBs created before FTS5 support: if
        # fts_guidelines is empty but guidelines has rows, bulk-populate it.
        fts_count = self.conn.execute(
            "SELECT COUNT(*) FROM fts_guidelines"
        ).fetchone()[0]
        if fts_count == 0:
            guideline_count = self.conn.execute(
                "SELECT COUNT(*) FROM guidelines"
            ).fetchone()[0]
            if guideline_count > 0:
                cur.execute(
                    "INSERT INTO fts_guidelines(id, text) "
                    "SELECT id, text FROM guidelines"
                )
        self.conn.commit()
        self._check_model_version()

    def _check_model_version(self) -> None:
        """Same embedding-space fingerprint check QdrantMemoryStore does: a db
        written by a different embedding model (same 384 dims) would silently
        return garbage neighbours. Probe one existing row's payload and hard-fail
        on a model_version mismatch rather than degrade retrieval quietly."""
        row = self.conn.execute(
            "SELECT payload FROM guidelines LIMIT 1"
        ).fetchone()
        if not row:
            return  # fresh/empty db -- nothing to validate against
        stored = json.loads(row[0]).get("model_version")
        if stored and stored != EMBEDDING_MODEL_VERSION:
            raise RuntimeError(
                f"sqlite memory db {str(self.path)!r} was written with embedding model "
                f"{stored!r}, but fabri is now using {EMBEDDING_MODEL_VERSION!r}. "
                f"Delete the db or point memory.sqlite_path at a fresh file."
            )

    def upsert(self, entry: MemoryEntry) -> str:
        vector = embed(entry.text)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO guidelines(id, text, kind, payload, hit_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry.id, entry.text, entry.kind, json.dumps(entry.to_payload()), entry.hit_count),
        )
        # vec0 doesn't support UPSERT cleanly across versions — delete then insert.
        cur.execute("DELETE FROM vec_guidelines WHERE id = ?", (entry.id,))
        cur.execute(
            "INSERT INTO vec_guidelines(id, embedding) VALUES (?, ?)",
            (entry.id, _pack(vector)),
        )
        # Keep FTS5 index in sync for BM25 retrieval.
        cur.execute("DELETE FROM fts_guidelines WHERE id = ?", (entry.id,))
        cur.execute(
            "INSERT INTO fts_guidelines(id, text) VALUES (?, ?)",
            (entry.id, entry.text),
        )
        self.conn.commit()
        return entry.id

    def get(self, point_id: str) -> MemoryEntry | None:
        row = self.conn.execute(
            "SELECT payload FROM guidelines WHERE id = ?", (point_id,)
        ).fetchone()
        if row is None:
            return None
        return MemoryEntry.from_payload(json.loads(row[0]))

    def query(
        self,
        text: str,
        top_k: int = 5,
        kind: str | None = None,
        tools_any: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        return self.query_by_vector(embed(text), top_k=top_k, kind=kind, tools_any=tools_any)

    def query_by_vector(
        self,
        vector: list[float],
        top_k: int = 5,
        kind: str | None = None,
        tools_any: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        # vec_guidelines.distance is cosine distance in [0, 2]; convert to a
        # cosine-similarity score in [-1, 1] for parity with QdrantMemoryStore.
        # Over-fetch when post-filtering by kind/tools so top_k results survive
        # the filter pass (4× headroom is plenty for small stores).
        fetch_k = top_k * 4 if (kind is not None or tools_any) else top_k
        rows = self.conn.execute(
            """SELECT v.id, v.distance, g.payload
                FROM vec_guidelines v
                JOIN guidelines g ON g.id = v.id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance""",
            (_pack(vector), fetch_k),
        ).fetchall()
        out: list[tuple[MemoryEntry, float]] = []
        for _id, distance, payload in rows:
            entry = MemoryEntry.from_payload(json.loads(payload))
            if kind is not None and entry.kind != kind:
                continue
            if tools_any is not None and not (set(entry.tools or []) & set(tools_any)):
                continue
            score = 1.0 - (distance / 2.0)  # cosine-distance → cosine-similarity
            out.append((entry, score))
            if len(out) >= top_k:
                break
        return out

    def query_bm25(
        self,
        text: str,
        top_k: int = 5,
        kind: str | None = None,
        tools_any: list[str] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        """BM25 full-text search via FTS5.

        Returns (entry, score) pairs where score is -bm25(fts_guidelines) so
        that higher is better (FTS5 bm25() returns negative values — more
        negative means more relevant). Degrades gracefully to [] on empty
        query or OperationalError."""
        fts_query = _fts5_query(text)
        if not fts_query:
            return []
        fetch_k = top_k * 4 if (kind is not None or tools_any) else top_k
        try:
            rows = self.conn.execute(
                """SELECT f.id, bm25(fts_guidelines) AS bm25_score, g.payload
                   FROM fts_guidelines f
                   JOIN guidelines g ON g.id = f.id
                   WHERE fts_guidelines MATCH ?
                   ORDER BY bm25_score
                   LIMIT ?""",
                (fts_query, fetch_k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[tuple[MemoryEntry, float]] = []
        for _id, bm25_score, payload_json in rows:
            entry = MemoryEntry.from_payload(json.loads(payload_json))
            if kind is not None and entry.kind != kind:
                continue
            if tools_any is not None and not (set(entry.tools or []) & set(tools_any)):
                continue
            out.append((entry, -bm25_score))  # negate: higher = more relevant
            if len(out) >= top_k:
                break
        return out

    def find_similar(
        self, text: str, threshold: float = 0.85, kind: str | None = None
    ) -> tuple[MemoryEntry, float] | None:
        results = self.query(text, top_k=1, kind=kind)
        if results and results[0][1] >= threshold:
            return results[0]
        return None

    def delete(self, point_id: str) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM guidelines WHERE id = ?", (point_id,))
        cur.execute("DELETE FROM vec_guidelines WHERE id = ?", (point_id,))
        cur.execute("DELETE FROM fts_guidelines WHERE id = ?", (point_id,))
        self.conn.commit()

    def count(self, kind: str | None = None) -> int:
        if kind is None:
            row = self.conn.execute("SELECT COUNT(*) FROM guidelines").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM guidelines WHERE kind = ?", (kind,)
            ).fetchone()
        return int(row[0])

    def iterate(
        self, kind: str | None = None, limit: int | None = None
    ) -> list[MemoryEntry]:
        if kind is None:
            sql = "SELECT payload FROM guidelines"
            args: tuple = ()
        else:
            sql = "SELECT payload FROM guidelines WHERE kind = ?"
            args = (kind,)
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, args).fetchall()
        return [MemoryEntry.from_payload(json.loads(r[0])) for r in rows]
