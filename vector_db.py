import os
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue

class QdrantStorage:
    def __init__(self, collection_name="financial_docs", dim=1024):
        url = os.getenv("QDRANT_URL", "http://localhost:6333")
        self.client = QdrantClient(url=url) 
        self.collection = collection_name
        
        self._ensure_collection(dim)

    def _ensure_collection(self, dim):
        collections = self.client.get_collections().collections
        exists = any(c.name == self.collection for c in collections)
        
        if not exists:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert_chunks(self, texts: list[str], vectors: list[list[float]], source_name: str):
        points = []
        for i, (text, vector) in enumerate(zip(texts, vectors)):
            points.append(PointStruct(
                id=str(uuid.uuid4()), # Generate a unique ID for every chunk
                vector=vector,
                payload={
                    "text": text, 
                    "source": source_name,
                    "chunk_index": i
                }
            ))
        
        self.client.upsert(collection_name=self.collection, points=points)

    def search(self, query_vector, top_k: int = 5, source_filter: str = None):
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
            limit=top_k
        )

        return [
            {
                "text": r.payload.get("text"),
                "source": r.payload.get("source"),
                "score": r.score
            } 
            for r in results
        ]