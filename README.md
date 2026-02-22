Financial RAG Agent

A local RAG (Retrieval-Augmented Generation) app that lets you ingest financial PDFs and ask questions about them. Built with FastAPI, Inngest, Qdrant, and Llama 3 LLM via Groq.

How it works

1. You send a PDF to the ingest pipeline — it gets chunked, embedded, and stored in Qdrant
2. You ask a question — it searches for the most relevant chunks and sends them to Llama 3 for an answer

Setup

Create a `.env` file in the project root:

```
GROQ_API_KEY=gsk_...
```

Place your PDF files in the `documents/` folder, then start everything:

- App: http://localhost:8000
- Inngest UI: http://localhost:8288
- Qdrant UI: http://localhost:6333/dashboard

Usage

Ingest a PDF

```json
{
  "data": {
    "pdf_path": "your_document.pdf",
    "source_id": "my_doc"
  }
}
```

Query a PDF

```json
{
  "data": {
    "question": "What is the total revenue for 2024?",
    "top_k": 5
  }
}
```


Useful commands

```bash
# Start everything
docker-compose up

# Rebuild after code changes
docker-compose build

```
