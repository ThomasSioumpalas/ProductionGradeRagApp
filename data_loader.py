import fitz 
from sentence_transformers import SentenceTransformer
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv

load_dotenv()


class FinancialDataLoader:
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 100):
        self._embed_model = None
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            # Adding separators ensures financial tables aren't split mid-line
            separators=["\n\n", "\n", ".", " ", ""] 
        )
        
    def _get_embed_model(self):
        if self._embed_model is None:
            self._embed_model = SentenceTransformer("BAAI/bge-m3")
        return self._embed_model


    def load_pdf_text(self, path: str) -> str:
        """Extracts full text from PDF as a single string."""
        parts: list[str] = []
        with fitz.open(path) as doc:
            for page in doc:
                parts.append(page.get_text())
        return "\n".join(parts).strip()

    def split_into_chunks(self, text: str) -> list[str]:
        """Splits the full text into manageable chunks for the LLM."""
        if not text or not text.strip():
            return []
        return self.text_splitter.split_text(text)

    def embed_texts(self, chunks: list[str], is_query: bool = False) -> list[list[float]]:
        """Converts chunks to 1024-dim vectors with BGE-M3 optimization."""
        if not chunks:
            return []

        # BGE-M3 performs better with a retrieval instruction for queries
        if is_query:
            processed_chunks = [f"Represent this query for retrieving financial metrics: {text}" for text in chunks]
        else:
            processed_chunks = chunks

        # Batch size 32 is a safe default for BGE-M3 on most systems
        embeddings = self._get_embed_model().encode(
            processed_chunks, 
            batch_size=32, 
            normalize_embeddings=True
        )
        return embeddings.tolist()

    def load_chunk_embed(self, path: str) -> tuple[list[str], list[list[float]]]:
        """Convenience pipeline for ingestion: PDF -> Chunks -> Vectors."""
        text = self.load_pdf_text(path)
        chunks = self.split_into_chunks(text)
        vectors = self.embed_texts(chunks)
        return chunks, vectors