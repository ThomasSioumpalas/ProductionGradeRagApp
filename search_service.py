from typing import List
from sentence_transformers import SentenceTransformer

class SearchService:
    def __init__(self, vector_store, embed_model: SentenceTransformer):
        self.vector_store = vector_store
        self.embed_model = embed_model

    def semantic_search(self, query: str, top_k: int = 5, source_file: str | None = None):
        query_vec = self.embed_model.encode([query])[0].tolist()
        return self.vector_store.search(query_vector=query_vec, top_k=top_k, source_file=source_file)