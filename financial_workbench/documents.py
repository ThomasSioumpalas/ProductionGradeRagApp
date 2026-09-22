"""PDF text extraction and bounded, page-aware chunks for large reports."""

import hashlib
import re
from pathlib import Path

import pymupdf

MAX_PAGES = 600
# Groq rejects a complete prompt (input plus allowed output) that exceeds the
# selected model's request limit. Keep one small source chunk per request.
DEFAULT_CHUNK_CHARS = 2_500
DEFAULT_CHUNK_OVERLAP = 250
DEFAULT_MAX_EXTRACTION_CHUNKS = 24
DEFAULT_BATCH_CHARS = 2_500
DEFAULT_BATCH_CHUNKS = 1

FINANCIAL_TERMS = (
    "statement of profit or loss", "statement of comprehensive income",
    "statement of financial position", "statement of cash flows",
    "statement of changes in equity", "revenue", "cost of sales",
    "gross profit", "operating profit", "finance costs", "income tax",
    "total assets", "total equity", "trade receivables", "inventories",
    "borrowings", "lease liabilities", "cash and cash equivalents",
    "net cash flow", "depreciation", "earnings per share",
)
PRIMARY_STATEMENT_TERMS = FINANCIAL_TERMS[:5]
NUMBER_PATTERN = re.compile(r"(?<!\w)(?:\(?-?\d{1,3}(?:[,. ]\d{3})+(?:[,.]\d+)?\)?|\(?-?\d+[,.]\d+\)?)")


def read_pdf(path: Path, filename: str, start_id: int = 0):
    """Read page text once without expensive table detection on every page."""
    pages, warnings = [], []
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError(f"Encrypted PDF: {filename}. Upload an unlocked copy.")
        if len(doc) > MAX_PAGES:
            raise ValueError(f"PDF exceeds {MAX_PAGES} pages: {filename}")
        for index, page in enumerate(doc):
            text = page.get_text("text", sort=True).strip()
            if len(text) < 40:
                warnings.append(f"{filename}, page {index + 1}: little or no readable text; OCR may be needed.")
            pages.append({"id": start_id + index, "file": filename, "sha256": digest,
                          "page": index + 1, "text": text, "tables": ""})
    if not pages or not any(len(p["text"]) >= 40 for p in pages):
        raise ValueError(f"No usable text in {filename}. Run OCR before uploading.")
    return pages, warnings


def page_windows(pages: list[dict], max_chars: int = DEFAULT_CHUNK_CHARS, overlap: int = DEFAULT_CHUNK_OVERLAP):
    """Split each page with overlap while preserving the original page citation."""
    if max_chars <= overlap:
        raise ValueError("Chunk overlap must be smaller than chunk size.")
    for page in pages:
        content = (page["text"] + "\n\n" + page.get("tables", "")).strip()
        if not content:
            continue
        for start in range(0, len(content), max_chars - overlap):
            chunk = content[start:start + max_chars]
            if chunk:
                yield page, chunk
            if start + max_chars >= len(content):
                break


def score_financial_chunk(page: dict, content: str) -> int:
    text = content.casefold()
    term_hits = sum(term in text for term in FINANCIAL_TERMS)
    primary_hits = sum(term in text for term in PRIMARY_STATEMENT_TERMS)
    numbers = len(NUMBER_PATTERN.findall(content))
    return primary_hits * 100 + term_hits * 10 + min(numbers, 25)


def select_extraction_windows(windows: list[tuple[dict, str]], max_chunks: int = DEFAULT_MAX_EXTRACTION_CHUNKS):
    """Keep a bounded, diverse set of financially relevant chunks.

    All PDF pages remain available to source viewing and Q&A. This selection only
    limits costly structured-model requests needed for workbook extraction.
    """
    if max_chunks < 1:
        raise ValueError("At least one extraction chunk is required.")
    if len(windows) <= max_chunks:
        return windows
    ranked = [(score_financial_chunk(page, content), index, page, content)
              for index, (page, content) in enumerate(windows)]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected, pages_seen = [], set()
    for score, index, page, content in ranked:
        source_page = (page["sha256"], page["page"])
        if score <= 0 or source_page in pages_seen:
            continue
        selected.append((index, page, content))
        pages_seen.add(source_page)
        if len(selected) == max_chunks:
            break
    if len(selected) < max_chunks:
        chosen = {item[0] for item in selected}
        for score, index, page, content in ranked:
            if score <= 0 or index in chosen:
                continue
            selected.append((index, page, content))
            chosen.add(index)
            if len(selected) == max_chunks:
                break
    if not selected:
        selected = [(index, page, content) for index, (page, content) in enumerate(windows[:max_chunks])]
    selected.sort(key=lambda item: item[0])
    return [(page, content) for _, page, content in selected]


def batch_windows(windows: list[tuple[dict, str]], max_chars: int = DEFAULT_BATCH_CHARS, max_chunks: int = DEFAULT_BATCH_CHUNKS):
    """Create bounded provider requests without splitting a source chunk."""
    batches, batch, size = [], [], 0
    for window in windows:
        content_size = len(window[1])
        if batch and (len(batch) >= max_chunks or size + content_size > max_chars):
            batches.append(batch)
            batch, size = [], 0
        batch.append(window)
        size += content_size
    if batch:
        batches.append(batch)
    return batches


def extraction_plan(pages: list[dict]):
    """Return all chunks, the bounded extraction selection, and provider batches."""
    windows = list(page_windows(pages))
    selected = select_extraction_windows(windows)
    return windows, selected, batch_windows(selected)
