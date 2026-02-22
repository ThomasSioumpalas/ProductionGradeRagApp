import logging
import os
import datetime
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

# Initialize the Inngest client
inngest_client = inngest.Inngest(
    app_id="rag_app",
    logger=logging.getLogger("uvicorn"),
    is_production=False,
    serializer=inngest.PydanticSerializer(),
)

# Initialize components
loader = FinancialDataLoader()
db_storage: QdrantStorage | None = None


def _extract_ingest_inputs(event_data: object) -> tuple[str, str | None]:
    """Accept either plain path strings or object payloads for ingestion events."""
    if isinstance(event_data, str):
        pdf_path = event_data.strip()
        source_id = None
    elif isinstance(event_data, dict):
        raw_path = (
            event_data.get("pdf_path")
            or event_data.get("path")
            or event_data.get("file_path")
            or event_data.get("pdf")
        )
        pdf_path = str(raw_path or "").strip()
        source_id = str(event_data.get("source_id") or "").strip() or None
    else:
        raise ValueError("event.data must be either a string path or an object with pdf_path")

    if not pdf_path:
        raise ValueError("A PDF path is required. Use a plain string or event.data.pdf_path")

    return pdf_path, source_id



def get_db_storage() -> QdrantStorage:
    """Lazy-init Qdrant so FastAPI/Inngest can boot even if Qdrant is still starting."""
    global db_storage
    if db_storage is None:
        db_storage = QdrantStorage(dim=1024)
    return db_storage


@inngest_client.create_function(
    fn_id="RAG: Ingest PDF",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf"),
    throttle=inngest.Throttle(limit=2, period=datetime.timedelta(minutes=1)),
)
async def rag_ingest_pdf(ctx: inngest.Context):
    def _load(ctx: inngest.Context) -> RAGChunkAndSrc:
        pdf_path, source_id = _extract_ingest_inputs(ctx.event.data)
        if not pdf_path:
            raise ValueError("event.data.pdf_path is required")

        resolved_source_id = source_id or Path(pdf_path.replace("\\", "/")).name

        text = loader.load_pdf_text(pdf_path)
        chunks = loader.split_into_chunks(text)

        return RAGChunkAndSrc(chunks=chunks, source_id=resolved_source_id)

    def _upsert(chunks_and_src: RAGChunkAndSrc) -> RAGUpsertResult:
        chunks = chunks_and_src.chunks
        source_id = chunks_and_src.source_id

        vecs = loader.embed_texts(chunks)  # is_query=False by default
        get_db_storage().upsert_chunks(texts=chunks, vectors=vecs, source_name=source_id)

        return RAGUpsertResult(ingested=len(chunks))

    chunks_and_src = await ctx.step.run(
        "load-and-chunk",
        lambda: _load(ctx),
        output_type=RAGChunkAndSrc,
    )
    ingested = await ctx.step.run(
        "embed-and-upsert",
        lambda: _upsert(chunks_and_src),
        output_type=RAGUpsertResult,
    )
    return ingested.model_dump()


@inngest_client.create_function(
    fn_id="RAG: Query PDF",
    trigger=inngest.TriggerEvent(event="rag/query_pdf"),
)
async def rag_query_pdf_ai(ctx: inngest.Context):
    def _search(question: str, top_k: int = 5) -> RAGSearchResult:
        query_vec = loader.embed_texts([question], is_query=True)[0]
        found = get_db_storage().search(query_vec, top_k)

        contexts = [item["text"] for item in found]
        sources = list({item["source"] for item in found})

        return RAGSearchResult(contexts=contexts, sources=sources)

    question = ctx.event.data["question"]
    top_k = int(ctx.event.data.get("top_k", 5))

    found = await ctx.step.run(
        "embed-and-search",
        lambda: _search(question, top_k),
        output_type=RAGSearchResult,
    )

    context_block = "\n\n".join(f"- {c}" for c in found.contexts)
    user_content = (
        "Use the following financial context to answer the question.\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n"
        "Return the answer in a clear format. If you find financial figures (Revenue, Net Income, etc.), list them clearly."
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


# ✅ Correct Inngest FastAPI integration for your installed SDK:
inngest.fast_api.serve(app, inngest_client, [rag_ingest_pdf, rag_query_pdf_ai])