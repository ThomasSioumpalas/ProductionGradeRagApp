import asyncio
import logging
import os
import datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pathlib import Path
import httpx
import inngest
import inngest.fast_api

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

thread_pool = ThreadPoolExecutor(max_workers=2)

_db_storage: QdrantStorage | None = None

def get_db_storage() -> QdrantStorage:
    global _db_storage
    if _db_storage is None:
        _db_storage = QdrantStorage(dim=384)
    return _db_storage


async def run_in_thread(fn):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, fn)


async def call_groq(messages: list, max_tokens: int = 1024, temperature: float = 0.1) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=30,
        )
        if response.status_code != 200:
            print(f"[Groq] Error {response.status_code}: {response.text}")
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


def _load_and_chunk(pdf_path: str, source_id: str | None) -> RAGChunkAndSrc:
    resolved_source_id = source_id or Path(pdf_path.replace("\\", "/")).name
    text = loader.load_pdf_text(pdf_path)
    chunks = loader.split_into_chunks(text)
    return RAGChunkAndSrc(chunks=chunks, source_id=resolved_source_id)


def _embed_and_upsert(chunks_and_src: RAGChunkAndSrc) -> RAGUpsertResult:
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


def _embed_and_search(question: str, top_k: int) -> RAGSearchResult:
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

    found = await ctx.step.run(
        "embed-and-search",
        lambda: run_in_thread(lambda: _embed_and_search(question, top_k)),
        output_type=RAGSearchResult,
    )

    context_block = "\n\n".join(f"- {c[:500]}" for c in found.contexts[:3])
    user_content = (
        "Use the following financial context to answer the question.\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n"
        "Return the answer in a clear format. If you find financial figures "
        "(Revenue, Net Income, etc.), list them clearly."
    )

    answer = await ctx.step.run(
        "llm-answer",
        lambda: call_groq(
            messages=[
                {"role": "system", "content": "You are a professional financial analyst assistant."},
                {"role": "user", "content": user_content},
            ]
        ),
    )

    return {"answer": answer, "sources": found.sources, "num_contexts": len(found.contexts)}


app = FastAPI()


@app.get("/")
def read_root():
    return {"status": "Financial Agent Online", "model": "MiniLM + Llama3"}


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