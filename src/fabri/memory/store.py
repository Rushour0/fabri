from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from fabri.memory.embeddings import EMBEDDING_DIM, embed
from fabri.memory.schema import MemoryEntry

COLLECTION_NAME = "fabri"


class QdrantMemoryStore:
    def __init__(self, url: str = "http://localhost:6333", collection: str = COLLECTION_NAME):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection not in existing:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qmodels.VectorParams(size=EMBEDDING_DIM, distance=qmodels.Distance.COSINE),
            )
            return

        # Existing collection: verify it matches the current embedding model's
        # dimension + distance. A mismatch (someone swapped the embedding model,
        # or pointed at a collection from a different project) would otherwise
        # surface as an opaque Qdrant error on upsert or, worse, return garbage
        # neighbors from an incompatible vector space.
        info = self.client.get_collection(self.collection)
        params = info.config.params.vectors
        # Qdrant returns either a VectorParams (single unnamed vector) or a
        # dict of named ones. We use the single-vector shape, so this is a
        # config error if it's anything else.
        if not isinstance(params, qmodels.VectorParams):
            raise RuntimeError(
                f"collection {self.collection!r} uses named vectors; fabri expects a single "
                f"unnamed vector. Recreate the collection or point fabri at a different one."
            )
        if params.size != EMBEDDING_DIM:
            raise RuntimeError(
                f"collection {self.collection!r} has vector size {params.size}, but the "
                f"current embedding model produces size {EMBEDDING_DIM}. Recreate the "
                f"collection or set memory.collection to a fresh name."
            )
        if params.distance != qmodels.Distance.COSINE:
            raise RuntimeError(
                f"collection {self.collection!r} uses distance {params.distance}, but fabri "
                f"expects cosine. Recreate the collection or use a different one."
            )

    def upsert(self, entry: MemoryEntry) -> str:
        vector = embed(entry.text)
        self.client.upsert(
            collection_name=self.collection,
            points=[qmodels.PointStruct(id=entry.id, vector=vector, payload=entry.to_payload())],
        )
        return entry.id

    def get(self, point_id: str) -> MemoryEntry | None:
        points = self.client.retrieve(collection_name=self.collection, ids=[point_id])
        if not points:
            return None
        return MemoryEntry.from_payload(points[0].payload)

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
        """Same as query() but accepts a precomputed embedding -- lets a caller
        embed once and run several filtered passes (see orchestrator/retrieval.py
        which does a vector pass plus one tag-filtered pass per mentioned tool)."""
        conditions = []
        if kind is not None:
            conditions.append(qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind)))
        if tools_any:
            conditions.append(qmodels.FieldCondition(key="tools", match=qmodels.MatchAny(any=tools_any)))
        query_filter = qmodels.Filter(must=conditions) if conditions else None
        results = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            query_filter=query_filter,
        )
        return [(MemoryEntry.from_payload(p.payload), p.score) for p in results.points]

    def find_similar(
        self, text: str, threshold: float = 0.85, kind: str | None = None
    ) -> tuple[MemoryEntry, float] | None:
        # kind=None searches every entry regardless of tactical/strategic, so a
        # recurrence of an already-promoted guideline still matches its
        # strategic entry instead of being re-inserted as a fresh tactical dup.
        results = self.query(text, top_k=1, kind=kind)
        if results and results[0][1] >= threshold:
            return results[0]
        return None

    def delete(self, point_id: str) -> None:
        self.client.delete(
            collection_name=self.collection,
            points_selector=qmodels.PointIdsList(points=[point_id]),
        )

    def count(self, kind: str | None = None) -> int:
        query_filter = None
        if kind is not None:
            query_filter = qmodels.Filter(
                must=[qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind))]
            )
        return self.client.count(collection_name=self.collection, count_filter=query_filter).count

    def iterate(
        self, kind: str | None = None, limit: int | None = None
    ) -> list[MemoryEntry]:
        """G2: stream every entry (optionally filtered by kind) so `fabri
        memory show` can list what's in the store. Uses Qdrant's scroll API
        rather than search to avoid embedding cost — the caller already knows
        what they want."""
        query_filter = None
        if kind is not None:
            query_filter = qmodels.Filter(
                must=[qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind))]
            )
        out: list[MemoryEntry] = []
        # `scroll` paginates by an opaque offset; loop until the server stops
        # returning a next-page token (or we hit `limit`).
        offset = None
        page_size = 128
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=query_filter,
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                out.append(MemoryEntry.from_payload(p.payload))
                if limit is not None and len(out) >= limit:
                    return out
            if offset is None:
                break
        return out
