from pathlib import Path

import fitz
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

load_dotenv()

PDF_SEARCH_DIRS = [
    Path("/app/documents"),
    Path("/app"),
    Path.cwd() / "documents",
    Path.cwd(),
]


class FinancialDataLoader:
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 100):
        self._embed_model = None
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", ".", " ", ""],
        )

    def _get_embed_model(self) -> SentenceTransformer:
        if self._embed_model is None:
            self._embed_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        return self._embed_model
    
    def _resolve_pdf_path(self, path: str) -> Path:
        direct = Path(path)
        if direct.exists() and direct.is_file():
            return direct

        filename = Path(path).name
        for directory in PDF_SEARCH_DIRS:
            candidate = directory / filename
            if candidate.exists() and candidate.is_file():
                return candidate

        available = []
        for directory in PDF_SEARCH_DIRS:
            if directory.exists():
                available.extend(p.name for p in directory.glob("*.pdf"))

        raise FileNotFoundError(
            f"PDF not found: '{path}'. "
            f"Available PDFs: {sorted(set(available)) or ['<none found>']}"
        )
        
        
        
        
        

    def load_pdf_text(self, path: str) -> str:
        resolved = self._resolve_pdf_path(path)
        with fitz.open(str(resolved)) as doc:
            return "\n".join(page.get_text() for page in doc)

    def split_into_chunks(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []
        return self.text_splitter.split_text(text)

    def embed_texts(self, chunks: list[str], is_query: bool = False) -> list[list[float]]:
        if not chunks:
            return []

        if is_query:
            chunks = [
                f"Represent this query for retrieving financial metrics: {text}"
                for text in chunks
            ]

        embeddings = self._get_embed_model().encode(
            chunks,
            batch_size=32,
            normalize_embeddings=True,
        )
        print(f"DEBUG: Embedding dimension is {len(embeddings[0])}")
        return embeddings.tolist()