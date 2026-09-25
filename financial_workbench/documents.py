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
DATE_PATTERN = re.compile(r"(?:\d{1,2}/\d{1,2}-)?\d{1,2}/\d{1,2}/\d{2,4}$")
YEAR_COLUMN = re.compile(r"20\d{2}(?:\([a-d]\))?", re.I)
AMOUNT_PATTERN = re.compile(r"(?:\(?[+\-−]?\d[\d.,]*\)?|0)$")
STATEMENT_TITLES = (
    ("income", re.compile(r"^\s*(?:[\w-]+\s+){0,3}?(?:consolidated\s+)?(?:statement of (?:profit or loss|comprehensive income|income)|income statement)\b", re.I | re.M)),
    ("balance", re.compile(r"^\s*(?:[\w-]+\s+){0,3}?(?:consolidated\s+)?(?:statement of financial position|balance sheet)\b", re.I | re.M)),
    ("equity", re.compile(r"^\s*(?:consolidated\s+)?statement of changes in equity\b", re.I | re.M)),
    ("cash", re.compile(r"^\s*(?:consolidated\s+)?(?:statement of cash flows|cash flow statement)\b", re.I | re.M)),
)
MAX_ROWS_PER_REQUEST = 16


def _split_spread(pdf_page):
    """Split a genuine two-page landscape spread at its empty central gutter."""
    width, height = pdf_page.rect.width, pdf_page.rect.height
    if width < height * 1.25:
        return False
    words = pdf_page.get_text("words")
    if len(words) < 60:
        return False
    left = sum(word[2] < width * .46 for word in words)
    right = sum(word[0] > width * .54 for word in words)
    gutter = sum(word[0] < width * .54 and word[2] > width * .46 for word in words)
    return left > 25 and right > 25 and gutter < len(words) * .015


def _lines(words):
    lines = []
    for word in sorted(words, key=lambda w: (w[1], w[0])):
        # Long table labels often wrap above and below their amount baseline.
        if lines and abs(word[1] - lines[-1]["last_y"]) < 6.2:
            lines[-1]["words"].append(word)
            lines[-1]["last_y"] = word[1]
        else:
            lines.append({"y": word[1], "last_y": word[1], "words": [word]})
    for line in lines:
        line["words"].sort(key=lambda w: w[0])
    return lines


def _year(token):
    match = re.search(r"((?:19|20)\d{2})(?!\d)", token)
    if not match:
        match = re.search(r"(\d{2})$", token)
    if not match:
        return None
    n = int(match.group(1))
    return n if n >= 1900 else (2000 + n if n < 80 else 1900 + n)


def _statement_title(text):
    start = text[:650].casefold()
    if "contents" in start[:180] or "notes to the financial statements" in start[:250]:
        return None
    for kind, pattern in STATEMENT_TITLES:
        if pattern.search(start):
            return kind
    return None


def _table_headers(lines, text):
    for line in lines:
        dates = [word for word in line["words"] if DATE_PATTERN.fullmatch(word[4])]
        if len(dates) not in (2, 4):
            # Many published filings use "2025 2024(a) 2023(a)" or
            # "Dec. 31, 2025  Dec. 31, 2024  Change". Reject isolated
            # narrative years: a table heading is short and has a unit nearby.
            dates = [word for word in line["words"] if YEAR_COLUMN.fullmatch(word[4])]
            if len(dates) not in (2, 3, 4) or len(line["words"]) > 18:
                continue
            if not _unit(text):
                continue
        # Infer each date's reporting scope from the printed headings above
        # its column, never from a generic use of "company" elsewhere on a page.
        headings = [word for heading in lines if line["y"] - 80 <= heading["y"] <= line["y"]
                    for word in heading["words"] if word[4].casefold() in
                    ("group", "consolidated", "company", "separate")]
        group = [word for word in headings if word[4].casefold() in ("group", "consolidated")]
        company = [word for word in headings if word[4].casefold() in ("company", "separate")]
        if group and company:
            boundary = (max(w[0] for w in group) + min(w[0] for w in company)) / 2
            if max(w[0] for w in group) > min(w[0] for w in company):
                boundary = (min(w[0] for w in group) + max(w[0] for w in company)) / 2
                scopes = ["consolidated" if w[0] > boundary else "standalone" for w in dates]
            else:
                scopes = ["consolidated" if w[0] < boundary else "standalone" for w in dates]
            if len(dates) == 4 and scopes.count("consolidated") != 2:
                continue
            if len(dates) == 2 and scopes[0] != scopes[1]:
                continue
        elif len(dates) in (2, 3) and (group or company):
            scopes = ["consolidated" if group else "standalone"] * len(dates)
        elif len(dates) in (2, 3) and re.search(r"\bconsolidated\b|\bunilever group\b", text[:1400], re.I):
            scopes = ["consolidated"] * len(dates)
        elif len(dates) in (2, 3) and re.search(r"\bstandalone\b|\bseparate financial statements\b", text[:1400], re.I):
            scopes = ["standalone"] * len(dates)
        else:
            continue
        return [
            {"x": (word[0] + word[2]) / 2, "start": word[0],
             "year": _year(word[4]), "scope": scope, "header": word[4],
             "y": line["y"]}
            for word, scope in zip(dates, scopes)
        ]
    return []


def _unit(text):
    opening = text[:2100].casefold()
    if "euro" in opening or "eur" in opening or "ευρώ" in opening or "€" in opening:
        currency = "EUR"
    elif "usd" in opening or "dollar" in opening or "us$" in opening:
        currency = "USD"
    elif "gbp" in opening or "pound sterling" in opening:
        currency = "GBP"
    else:
        return None
    if re.search(r"(?:000['’]s|\b0{3}\b|thousands?|χιλιάδ)", opening):
        scale = 1000
    elif re.search(r"(?:millions?|\bmn\b|€\s?m\b|εκατομμύρ)", opening):
        scale = 1000000
    elif re.search(r"\b(in|amounts? in)\s+(?:euros?|eur|usd|gbp)\b", opening):
        scale = 1
    else:
        return None
    return currency, scale


def _table_rows(lines, headers, panel, page, include_unlabelled=False):
    # Accounting columns right-align the number under a short year heading.
    # Large amounts can begin to the left of the heading's first character.
    first = min(header["start"] for header in headers) - 16
    rows, section = [], ""
    for line in lines:
        if line["y"] < headers[0]["y"] + 8:
            continue
        words = line["words"]
        label_words = sorted((word for word in words if word[0] < first), key=lambda w: (w[1], w[0]))
        label = " ".join(word[4] for word in label_words).strip()
        numbers = [word for word in words if word[0] >= first and AMOUNT_PATTERN.fullmatch(word[4])]
        if not numbers:
            if label and any(key in label.casefold() for key in (
                "current assets", "current liabilities", "non-current assets",
                "non-current liabilities", "operating activities", "investing activities",
                "financing activities", "shareholders' equity", "equity",
                "other comprehensive income", "earnings per share",
            )):
                section = label
            continue
        if len(numbers) != len(headers) or (not include_unlabelled and not re.search(r"[^\W\d_]", label)):
            continue
        # Note references occupy the narrow gap just before the amount columns.
        if len(label_words) > 1 and label_words[-1][0] > first - 85 and re.fullmatch(r"\d+(?:,\d+)?", label_words[-1][4]):
            label = " ".join(word[4] for word in label_words[:-1]).strip()
        values = []
        for header, word in zip(headers, numbers):
            if abs((word[0] + word[2]) / 2 - header["x"]) > 45:
                break
            values.append({"year": header["year"], "scope": header["scope"],
                           "header": header["header"], "raw_value": word[4],
                           "x": round(word[0], 1)})
        if len(values) != len(headers):
            continue
        row = {
            "row_id": f"{page['page']}:{panel['side']}:{len(rows)}",
            "page": page["page"], "file": page["file"], "document_id": page["sha256"],
            "panel": panel["side"],
            "statement": panel["statement"], "section": section, "label": label,
            "quote": " ".join(word[4] for word in label_words + numbers),
            "currency": panel["currency"], "scale": panel["scale"],
            "y": round(line["y"], 1),
            "values": values,
        }
        rows.append(row)
        if label.casefold() in ("profit after tax", "total comprehensive income"):
            section = label
    return rows


def _annotate_statements(pages):
    active = None
    in_notes = False
    seen_statements = False
    document = None
    for page in pages:
        if page["sha256"] != document:
            active, in_notes, document = None, bool(re.search(r"\bnotes?\b", page["file"], re.I)), page["sha256"]
            seen_statements = False
        for panel in page["panels"]:
            text = panel["text"]
            note_heading = re.search(r"\bnotes\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements\b", text[:500], re.I)
            if "contents" in text[:200].casefold() or (seen_statements and note_heading):
                active = None
            if seen_statements and note_heading:
                in_notes = True
            heading = _statement_title(text)
            if heading and not in_notes:
                active = heading
            lines = _lines(panel.pop("_words"))
            headers = _table_headers(lines, text)
            unit = _unit(text)
            if active and headers and unit:
                panel["statement"] = active
                panel["headers"] = [{k: v for k, v in h.items() if k != "y"} for h in headers]
                panel["currency"], panel["scale"] = unit
                panel["rows"] = _table_rows(lines, headers, panel, page)
                if panel["rows"]:
                    seen_statements = True
                page["tables"] += "\n".join(row["quote"] for row in panel["rows"]) + "\n"
            else:
                is_continuation = active and re.search(r"\b(group|company)\b", text[:240], re.I)
                panel.update(statement=active if heading or is_continuation else None,
                             headers=[], rows=[])
                # Financial statement notes contain dated tables that were
                # previously indexed as prose but excluded from extraction.
                # The heading and coordinates keep similarly named rows in
                # different notes from being conflated.
                if (in_notes or not active) and headers and unit and "contents" not in text[:200].casefold():
                    note_panel = {"side": panel["side"], "statement": "note",
                                  "currency": unit[0], "scale": unit[1]}
                    titles = [(line["y"], " ".join(w[4] for w in line["words"]))
                              for line in lines if re.match(r"^\s*\d{1,2}\.\s+[A-Za-zΑ-Ωα-ω]",
                                                     " ".join(w[4] for w in line["words"]))]
                    panel["note_rows"] = _table_rows(lines, headers, note_panel, page,
                                                       include_unlabelled=True)
                    for row in panel["note_rows"]:
                        row["note_title"] = next((title for y, title in reversed(titles) if y < row["y"]), "")
                    panel["note_headers"] = [{k: v for k, v in h.items() if k != "y"} for h in headers]
                    page["tables"] += "\n".join(row["quote"] for row in panel["note_rows"]) + "\n"


def statement_plan(pages):
    """Select all detected statement rows, regardless of where they occur in a PDF."""
    panels = [panel | {"file": page["file"], "page": page["page"]}
              for page in pages for panel in page.get("panels", []) if panel.get("rows")]
    tasks = []
    for panel in panels:
        rows = panel["rows"]
        for start in range(0, len(rows), MAX_ROWS_PER_REQUEST):
            tasks.append(rows[start:start + MAX_ROWS_PER_REQUEST])
    return panels, tasks


def read_pdf(path: Path, filename: str, start_id: int = 0):
    """Read every page once, preserving separate columns on two-page spreads."""
    pages, warnings = [], []
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError(f"Encrypted PDF: {filename}. Upload an unlocked copy.")
        if len(doc) > MAX_PAGES:
            raise ValueError(f"PDF exceeds {MAX_PAGES} pages: {filename}")
        for index, page in enumerate(doc):
            width, height = page.rect.width, page.rect.height
            clips = (
                [("left", pymupdf.Rect(0, 0, width / 2, height)),
                 ("right", pymupdf.Rect(width / 2, 0, width, height))]
                if _split_spread(page) else [("full", page.rect)]
            )
            panels = [{"side": side, "text": page.get_text("text", clip=clip, sort=True).strip(),
                       "_words": page.get_text("words", clip=clip)} for side, clip in clips]
            text = "\n\n".join(f"[{p['side']}]\n{p['text']}" for p in panels)
            if len(text) < 40:
                warnings.append(f"{filename}, page {index + 1}: little or no readable text; OCR may be needed.")
            pages.append({"id": start_id + index, "file": filename, "sha256": digest,
                          "page": index + 1, "text": text, "tables": "", "panels": panels})
    _annotate_statements(pages)
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
