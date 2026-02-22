import os
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams,
    Distance,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)


class QdrantStorage:
    def __init__(self, collection_name: str = "financial_docs", dim: int = 1024):
        url = os.getenv("QDRANT_URL", "http://localhost:6333")
        self.client = QdrantClient(url=url)
        self.collection = collection_name
        self._ensure_collection(dim)

    def _ensure_collection(self, dim: int) -> None:
        existing = [c.name for c in self.client.get_collections().collections]
        if self.collection not in existing:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert_chunks(self, texts: list[str], vectors: list[list[float]], source_name: str) -> None:
        if not texts or not vectors:
            print("[QdrantStorage] Nothing to upsert, skipping")
            return

        if len(texts) != len(vectors):
            raise ValueError(f"Mismatch: {len(texts)} texts vs {len(vectors)} vectors")

        points = []
        for i, (text, vector) in enumerate(zip(texts, vectors)):
            if not text or not text.strip():
                continue

            # ✅ Use UUID5 — deterministic (same source+index = same ID) and valid for Qdrant
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_name}_{i}"))
            points.append(
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={"text": text, "source": source_name, "chunk_index": i},
                )
            )

        if not points:
            print("[QdrantStorage] All chunks were empty, skipping upsert")
            return

        print(f"[QdrantStorage] Upserting {len(points)} points into '{self.collection}'")
        self.client.upsert(collection_name=self.collection, points=points)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        source_filter: str | None = None,
    ) -> list[dict]:
        search_filter = None
        if source_filter:
            search_filter = Filter(
                must=[FieldCondition(key="source", match=MatchValue(value=source_filter))]
            )

        results = self.client.search(
            collection_name=self.collection,
            query_vector=query_vector,
            query_filter=search_filter,
            with_payload=True,
            limit=top_k,
        )

        return [
            {"text": r.payload.get("text"), "source": r.payload.get("source"), "score": r.score}
            for r in results
        ]