Event-Driven RAG Pipeline with FastAPI, Inngest, and Qdrant

A Retrieval-Augmented Generation (RAG) application that allows users to upload PDF documents, store their content as vector embeddings, and ask questions based strictly on those documents.

The system retrieves relevant text chunks and uses an LLM to generate grounded, context-aware answers — reducing hallucinations and improving response accuracy.

Overview

This application demonstrates a production-style RAG workflow built around asynchronous event processing.

Core capabilities:

- Upload PDF documents  
- Extract and chunk text  
- Generate embeddings  
- Store vectors in Qdrant  
- Retrieve relevant context  
- Generate document-based answers  


Tech Stack

Backend
- FastAPI
- Inngest
- OpenAI Python SDK
- LlamaIndex
- Qdrant

Frontend
- Streamlit

Infrastructure
- python-dotenv
- Uvicorn

System Architecture
PDF Ingestion Flow

1. User uploads a PDF via Streamlit  
2. Streamlit sends a `rag/ingest_pdf` event to Inngest  
3. The Inngest function:
   - Extracts PDF text  
   - Splits text into chunks  
   - Converts chunks into embeddings  
   - Upserts vectors into Qdrant  

Question Answering Flow

1. User submits a question  
2. The application queries Qdrant for relevant chunks  
3. Retrieved context is sent to the LLM  
4. The model generates an answer grounded in the stored document data  

Setup Guide

1. Install Dependencies

This repository uses pyproject.toml.

Install with your preferred package manager:

```bash
uv sync

