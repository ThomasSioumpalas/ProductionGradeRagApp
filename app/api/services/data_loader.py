from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.readers.file import PDFReader
from llama_index.core.node_parser import SentenceSplitter

embed_model = OllamaEmbedding(model_name="nomic-embed-text")
splitter = SentenceSplitter(chunk_size=1000, chunk_overlap=200)

def load_and_chunk_pdf(path: str) -> list[str]:
    docs = PDFReader().load_data(file=path)
    texts = [d.text for d in docs if getattr(d, "text", None)]
    chunks: list[str] = []
    for t in texts:
        chunks.extend(splitter.split_text(t))
    return chunks

def embed_texts(texts: list[str]) -> list[list[float]]:
    return [embed_model.get_text_embedding(t) for t in texts]
