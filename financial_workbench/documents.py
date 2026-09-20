"""Page-aware chunks preserve provenance and table column order."""

import hashlib
from pathlib import Path

import pymupdf

MAX_PAGES = 600


def read_pdf(path: Path, filename: str, start_id: int = 0):
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
                warnings.append(
                    f"{filename}, page {index + 1}: little or no readable text; OCR may be needed."
                )
            # Tables complement, rather than replace, the original page text.
            tables = []
            try:
                for table in page.find_tables().tables:
                    tables.append(table.to_markdown())
            except (ValueError, RuntimeError):
                warnings.append(
                    f"{filename}, page {index + 1}: table detection failed; text retained."
                )
            pages.append(
                {
                    "id": start_id + index,
                    "file": filename,
                    "sha256": digest,
                    "page": index + 1,
                    "text": text,
                    "tables": "\n\n".join(tables),
                }
            )
    if not pages or not any(len(p["text"]) >= 40 for p in pages):
        raise ValueError(f"No usable text in {filename}. Run OCR before uploading.")
    return pages, warnings


def page_windows(pages: list[dict], max_chars=14000):
    """Process every page; overlap oversized pages without losing page identity."""
    for p in pages:
        content = p["text"] + "\n\n" + p["tables"]
        for start in range(0, max(1, len(content)), max_chars - 1000):
            yield p, content[start : start + max_chars]
