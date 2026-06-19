from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from agent_memory.memory.embeddings import EMBEDDING_DIM, embed
from agent_memory.memory.schema import MemoryEntry

COLLECTION_NAME = "agent_memory"


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
        vector = embed(text)
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
