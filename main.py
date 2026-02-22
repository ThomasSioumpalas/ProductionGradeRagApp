import asyncio
import logging
import os
import datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pathlib import Path
import inngest
import inngest.fast_api
from inngest.experimental import ai

from data_loader import FinancialDataLoader
from vector_db import QdrantStorage
from kpis import FinancialKPIs, save_to_excel
from custom_types import RAQQueryResult, RAGSearchResult, RAGUpsertResult, RAGChunkAndSrc

load_dotenv()

inngest_client = inngest.Inngest(
    app_id="rag_app",
    logger=logging.getLogger("uvicorn"),
    is_production=False,
    serializer=inngest.PydanticSerializer(),
)

loader = FinancialDataLoader()

# Thread pool for CPU-heavy blocking work (PDF parsing, embedding)
# This keeps the async event loop free so FastAPI can still handle
# Inngest's sync pings while a step is running.
thread_pool = ThreadPoolExecutor(max_workers=2)

# Simple lazy singleton
_db_storage: QdrantStorage | None = None

def get_db_storage() -> QdrantStorage:
    global _db_storage
    if _db_storage is None:
        _db_storage = QdrantStorage(dim=1024)
    return _db_storage


async def run_in_thread(fn):
    """Run a blocking sync function in the thread pool without blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, fn)


# ---------------------------------------------------------------------------
# Ingest PDF
# ---------------------------------------------------------------------------

def _load_and_chunk(pdf_path: str, source_id: str | None) -> RAGChunkAndSrc:
    """Blocking: load PDF from disk and split into chunks."""
    resolved_source_id = source_id or Path(pdf_path.replace("\\", "/")).name
    text = loader.load_pdf_text(pdf_path)
    chunks = loader.split_into_chunks(text)
    return RAGChunkAndSrc(chunks=chunks, source_id=resolved_source_id)


def _embed_and_upsert(chunks_and_src: RAGChunkAndSrc) -> RAGUpsertResult:
    """Blocking: run BGE-M3 embeddings and store vectors in Qdrant."""
    vecs = loader.embed_texts(chunks_and_src.chunks)
    get_db_storage().upsert_chunks(
        texts=chunks_and_src.chunks,
        vectors=vecs,
        source_name=chunks_and_src.source_id,
    )
    return RAGUpsertResult(ingested=len(chunks_and_src.chunks))


@inngest_client.create_function(
    fn_id="RAG: Ingest PDF",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf"),
    throttle=inngest.Throttle(limit=2, period=datetime.timedelta(minutes=1)),
)
async def rag_ingest_pdf(ctx: inngest.Context):
    # Extract inputs once, before any steps
    event_data = ctx.event.data
    if isinstance(event_data, str):
        pdf_path = event_data.strip()
        source_id = None
    elif isinstance(event_data, dict):
        pdf_path = str(
            event_data.get("pdf_path")
            or event_data.get("path")
            or event_data.get("file_path")
            or event_data.get("pdf")
            or ""
        ).strip()
        source_id = str(event_data.get("source_id") or "").strip() or None
    else:
        raise ValueError("event.data must be a string path or an object with pdf_path")

    if not pdf_path:
        raise ValueError("A PDF path is required")

    # ✅ run_in_thread keeps the event loop free during heavy CPU work.
    # Inngest's sync pings (PUT /api/inngest) can still be answered
    # while PDF parsing and embedding run in the background thread.
    chunks_and_src = await ctx.step.run(
        "load-and-chunk",
        lambda: run_in_thread(lambda: _load_and_chunk(pdf_path, source_id)),
        output_type=RAGChunkAndSrc,
    )
    result = await ctx.step.run(
        "embed-and-upsert",
        lambda: run_in_thread(lambda: _embed_and_upsert(chunks_and_src)),
        output_type=RAGUpsertResult,
    )
    return result.model_dump()


# ---------------------------------------------------------------------------
# Query PDF
# ---------------------------------------------------------------------------

def _embed_and_search(question: str, top_k: int) -> RAGSearchResult:
    """Blocking: embed query with BGE-M3 and search Qdrant."""
    query_vec = loader.embed_texts([question], is_query=True)[0]
    found = get_db_storage().search(query_vec, top_k)
    contexts = [item["text"] for item in found]
    sources = list({item["source"] for item in found})
    return RAGSearchResult(contexts=contexts, sources=sources)


@inngest_client.create_function(
    fn_id="RAG: Query PDF",
    trigger=inngest.TriggerEvent(event="rag/query_pdf"),
)
async def rag_query_pdf_ai(ctx: inngest.Context):
    question = ctx.event.data["question"].strip()
    top_k = int(ctx.event.data.get("top_k", 5))

    # ✅ Same pattern — embedding runs in thread, event loop stays responsive
    found = await ctx.step.run(
        "embed-and-search",
        lambda: run_in_thread(lambda: _embed_and_search(question, top_k)),
        output_type=RAGSearchResult,
    )

    context_block = "\n\n".join(f"- {c}" for c in found.contexts)
    user_content = (
        "Use the following financial context to answer the question.\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n"
        "Return the answer in a clear format. If you find financial figures "
        "(Revenue, Net Income, etc.), list them clearly."
    )

    adapter = ai.openai.Adapter(
        auth_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-mini",
    )

    res = await ctx.step.ai.infer(
        "llm-answer",
        adapter=adapter,
        body={
            "max_tokens": 1024,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "You are a professional financial analyst assistant."},
                {"role": "user", "content": user_content},
            ],
        },
    )

    answer = res["choices"][0]["message"]["content"].strip()
    return {"answer": answer, "sources": found.sources, "num_contexts": len(found.contexts)}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()


@app.get("/")
def read_root():
    return {"status": "Financial Agent Online", "model": "BGE-M3"}


@app.post("/download-report")
async def download_report(data: dict):
    try:
        analyzer = FinancialKPIs(data)
        df = analyzer.to_dataframe()
        file_path = save_to_excel(df, filename="Financial_Analysis_Report.xlsx")
        return FileResponse(
            path=file_path,
            filename="Financial_Analysis_Report.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


inngest.fast_api.serve(app, inngest_client, [rag_ingest_pdf, rag_query_pdf_ai])