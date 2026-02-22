from data_loader import FinancialDataLoader
from vector_db import QdrantStorage


class SearchService:
    def __init__(self, vector_store: QdrantStorage, loader: FinancialDataLoader):
        self.vector_store = vector_store
        self.loader = loader

    def semantic_search(
        self,
        query: str,
        top_k: int = 5,
        source_file: str | None = None,
    ) -> list[dict]:
        # ✅ Reuses the shared loader — no duplicate model loading
        query_vec = self.loader.embed_texts([query], is_query=True)[0]
        return self.vector_store.search(query_vector=query_vec, top_k=top_k, source_filter=source_file)