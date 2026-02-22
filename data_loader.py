import re
from pathlib import Path

import fitz
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

load_dotenv()


class FinancialDataLoader:
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 100):
        self._embed_model = None
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            # Adding separators ensures financial tables aren't split mid-line
            separators=["\n\n", "\n", ".", " ", ""],
        )
        
    def _get_embed_model(self):
        if self._embed_model is None:
            self._embed_model = SentenceTransformer("BAAI/bge-m3")
        return self._embed_model

    def _resolve_pdf_path(self, path: str) -> Path:
        raw_path = str(path).strip().strip('"').strip("'")
        if raw_path.lower().startswith("file://"):
            raw_path = raw_path[7:]

        linux_like = raw_path.replace("\\", "/")
        windows_drive = re.match(r"^[A-Za-z]:/", linux_like) is not None
        relative_from_windows = linux_like.lstrip("/") if windows_drive else linux_like
        requested = Path(relative_from_windows)
        basename = Path(linux_like).name or requested.name

        candidates: list[Path] = [Path(raw_path)]
        if windows_drive:
            candidates.append(Path("/") / relative_from_windows)

        if requested.is_absolute():
            candidates.append(requested)
        else:
            candidates.extend([Path.cwd() / requested, Path("/app") / requested])

        if basename:
            candidates.extend(
                [
                    Path.cwd() / "documents" / basename,
                    Path("/app/documents") / basename,
                    Path.cwd() / basename,
                    Path("/app") / basename,
                ]
            )

        seen: set[Path] = set()
        for candidate in candidates:
            c = candidate.resolve(strict=False)
            if c in seen:
                continue
            seen.add(c)
            if c.exists() and c.is_file():
                return c

        searchable_dirs = [Path.cwd() / "documents", Path("/app/documents"), Path.cwd(), Path("/app")]
        nearby: list[str] = []
        for directory in searchable_dirs:
            if directory.exists() and directory.is_dir():
                nearby.extend(sorted(p.name for p in directory.glob("*.pdf"))[:10])

        looked_in = ", ".join(str(p) for p in seen) if seen else str(requested)
        available = sorted(set(nearby)) if nearby else ["<no PDFs discovered>"]
        raise FileNotFoundError(
            f"Missing PDF: {path}. Looked in: {looked_in}. Available PDFs: {available}"
        )


    def load_pdf_text(self, path: str) -> str:
        resolved_path = self._resolve_pdf_path(path)
        with fitz.open(str(resolved_path)) as doc:
            return "\n".join(page.get_text() for page in doc)
        
        
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
            processed_chunks = [
                f"Represent this query for retrieving financial metrics: {text}" for text in chunks
            ]
        else:
            processed_chunks = chunks

        # Batch size 32 is a safe default for BGE-M3 on most systems
        embeddings = self._get_embed_model().encode(
            processed_chunks, 
            batch_size=32, 
            normalize_embeddings=True,
        )
        return embeddings.tolist()

    def load_chunk_embed(self, path: str) -> tuple[list[str], list[list[float]]]:
        """Convenience pipeline for ingestion: PDF -> Chunks -> Vectors."""
        text = self.load_pdf_text(path)
        chunks = self.split_into_chunks(text)
        vectors = self.embed_texts(chunks)
        return chunks, vectors