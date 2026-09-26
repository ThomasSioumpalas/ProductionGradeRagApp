"""PDF text extraction and bounded, page-aware chunks for large reports."""

import hashlib
import re
import unicodedata
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
# Year-end dates (31/12/2025, 31.12.2025) and period columns
# (1/1-31/12/2025, 01.01-31.12.2025, 01.01.2025-31.12.2025).
DATE_PATTERN = re.compile(r"(?:\d{1,2}[./]\d{1,2}(?:[./](?:\d{2,4})?)?[-–])?\d{1,2}[./]\d{1,2}[./]\d{2,4}$")
YEAR_COLUMN = re.compile(r"20\d{2}(?:\([a-d]\))?", re.I)
AMOUNT_PATTERN = re.compile(r"(?:\(?[+\-−]?\d[\d.,]*\)?|0)$")
_TITLE_PREFIX = r"^\s*(?:\d{1,2}[.)]\s*)?(?:[\w-]+\s+){0,3}?(?:consolidated\s+|separate\s+)?"
# Titles are matched on accent-stripped, casefolded text, so Greek headings
# printed in capitals (ΚΑΤΑΣΤΑΣΗ ΤΑΜΕΙΑΚΩΝ ΡΟΩΝ) match their accented form.
STATEMENT_TITLES = (
    ("income", re.compile(_TITLE_PREFIX + r"(?:statement of (?:profit or loss|comprehensive income|income)|income statement"
                          r"|καταστ\w* (?:αποτελεσματων|συνολικου εισοδηματος))\b", re.I | re.M)),
    ("balance", re.compile(_TITLE_PREFIX + r"(?:statement of financial position|balance sheet"
                           r"|καταστ\w* χρηματοοικονομικης θεσης|ισολογισμος)\b", re.I | re.M)),
    ("equity", re.compile(_TITLE_PREFIX + r"(?:statement of changes in (?:[\w’']+\s+)?equity"
                          r"|καταστ\w* μεταβολων (?:των )?ιδιων κεφαλαιων)\b", re.I | re.M)),
    ("cash", re.compile(_TITLE_PREFIX + r"(?:statement of cash flows?|cash flows? statement"
                        r"|καταστ\w* ταμειακων ροων)\b", re.I | re.M)),
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
    # A period column is labelled by its end date (01.07.2024-30.06.2025).
    match = next(reversed(list(re.finditer(r"((?:19|20)\d{2})(?!\d)", token))), None)
    if not match:
        match = re.search(r"(\d{2})$", token)
    if not match:
        return None
    n = int(match.group(1))
    return n if n >= 1900 else (2000 + n if n < 80 else 1900 + n)


def _plain(text):
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _statement_title(text):
    start = _plain(text[:650])
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


_CURRENCY = r"(?:euros?|eur|ευρώ|usd|dollars?|gbp|pounds?|€|\$|£)"
_CODE = r"(?:€|\$|£|\b(?:euros?|eur|usd|gbp|dollars?))"
# Only an explicit presentation-unit phrase sets the scale. A bare "000" inside
# a printed amount (349.523.000, 1,000) or the word "thousand" in prose is not
# a declaration; the earliest declaration on the page is the table heading.
UNIT_DECLARATIONS = (
    (1000, re.compile(
        r"thousands?\s*(?:of\s*)?" + _CURRENCY
        + r"|" + _CODE + r"\s*thousands?\b"
        + r"|" + _CODE + r"\s*['’]?\s*000(?![\d.,]*\d)"
        + r"|(?<![\d.,'’])['’]?000['’]?s\b"
        + r"|\bin\s*thousands?\b|\b(?:teur|keur)\b|\bk€|€\s*k\b"
        + r"|χιλ(?:ιάδες|\.)?\s*(?:ευρώ|€)|ποσά\s*σε\s*χιλ", re.I)),
    (1000000, re.compile(
        r"millions?\s*(?:of\s*)?" + _CURRENCY
        + r"|" + _CODE + r"\s*(?:millions?|mn|mio|m)\b"
        + r"|\bin\s*millions?\b"
        + r"|εκατ(?:ομμύρια|\.)?\s*(?:ευρώ|€)|ποσά\s*σε\s*εκατ", re.I)),
    (1, re.compile(
        r"amounts?\s*(?:are\s*)?(?:expressed\s*|presented\s*|stated\s*|shown\s*)?in\s*"
        + _CURRENCY + r"(?![^\W\d_])(?!\s*(?:thousands?|millions?|['’]?000|mn\b|mio\b|m\b|k\b))"
        + r"|\(\s*in\s*" + _CURRENCY + r"\s*\)"
        + r"|ποσά\s*(?:σε|εκφρασμένα\s*σε)\s*(?:ευρώ|€)", re.I)),
)
# Weaker wording, used only when no explicit declaration is present.
UNIT_FALLBACK = re.compile(
    r"\bin\s*(?:euros?|eur|usd|gbp)\b(?!\s*(?:thousands?|millions?|['’]?000|mn\b|m\b))", re.I)


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
    found = [(match.start(), scale) for scale, pattern in UNIT_DECLARATIONS
             if (match := pattern.search(opening))]
    if found:
        return currency, min(found)[1]
    if UNIT_FALLBACK.search(opening):
        return currency, 1
    return None


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
        numbers = [word for word in words if word[0] >= first and (AMOUNT_PATTERN.fullmatch(word[4]) or word[4] in ("-", "–", "—"))]
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
        if len(label_words) > 1 and label_words[-1][0] > first - 85 and re.fullmatch(r"\d+(?:[.,]\d+)?[a-z]?", label_words[-1][4], re.I):
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
            "unit_source": panel.get("unit_source", ""),
            "y": round(line["y"], 1),
            "values": values,
        }
        rows.append(row)
        if label.casefold() in ("profit after tax", "total comprehensive income"):
            section = label
    return rows


def _annotate_statements(pages):
    """Label statement and note tables; return document-level unit warnings."""
    active = None
    in_notes = False
    seen_statements = False
    document = None
    # A statement's own unit heading is carried to (a) the immediately
    # following continuation page of the same statement and (b) later note
    # tables in the same PDF that print no unit of their own. Notes follow the
    # statements' presentation unit unless a page declares otherwise.
    statement_unit, previous_statement, units_by_document = None, None, {}
    for page in pages:
        if page["sha256"] != document:
            active, in_notes, document = None, bool(re.search(r"\bnotes?\b", page["file"], re.I)), page["sha256"]
            seen_statements = False
            statement_unit, previous_statement = None, None
        for panel in page["panels"]:
            text = panel["text"]
            note_heading = re.search(r"\bnotes\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements\b", text[:500], re.I)
            numbered_notes = re.search(
                r"(?m)^\s*(?:5\.\s+General information|6\.\s+Basis of preparation|7\.\s+Detailed data)",
                text[:650], re.I
            )
            if "contents" in text[:200].casefold() or (seen_statements and (note_heading or numbered_notes)):
                active = None
            if seen_statements and (note_heading or numbered_notes):
                in_notes = True
            heading = _statement_title(text)
            if heading and not in_notes:
                active = heading
            lines = _lines(panel.pop("_words"))
            headers = _table_headers(lines, text)
            unit = _unit(text)
            unit_source = f"declared on page {page['page']}" if unit else ""
            years = [(h["year"], h["scope"]) for h in headers]
            if (not unit and active and headers and statement_unit and previous_statement
                    and previous_statement[1] == active and previous_statement[2] == years
                    and page["page"] - previous_statement[0] in (0, 1)):
                unit, unit_source = statement_unit
            if active and headers and unit:
                panel["statement"] = active
                panel["headers"] = [{k: v for k, v in h.items() if k != "y"} for h in headers]
                panel["currency"], panel["scale"] = unit
                panel["unit_source"] = unit_source
                panel["rows"] = _table_rows(lines, headers, panel, page)
                if panel["rows"]:
                    seen_statements = True
                    previous_statement = (page["page"], active, years)
                    if unit_source.startswith("declared"):
                        statement_unit = (unit, f"inherited from page {page['page']}")
                        units_by_document.setdefault((page["file"], document), {}).setdefault(
                            unit, []).append(page["page"])
                page["tables"] += "\n".join(row["quote"] for row in panel["rows"]) + "\n"
            else:
                is_continuation = active and re.search(r"\b(group|company)\b", text[:240], re.I)
                panel.update(statement=active if heading or is_continuation else None,
                             headers=[], rows=[])
                if not unit and seen_statements and statement_unit:
                    unit, unit_source = statement_unit
                # Financial statement notes contain dated tables that were
                # previously indexed as prose but excluded from extraction.
                # The heading and coordinates keep similarly named rows in
                # different notes from being conflated.
                if (in_notes or not active) and headers and unit and "contents" not in text[:200].casefold():
                    note_panel = {"side": panel["side"], "statement": "note",
                                  "currency": unit[0], "scale": unit[1],
                                  "unit_source": unit_source}
                    titles = [(line["y"], " ".join(w[4] for w in line["words"]))
                              for line in lines if re.match(r"^\s*\d{1,2}(?:\.\d{1,2}[a-z]?)?\.?\s+[A-Za-zΑ-Ωα-ω]",
                                                     " ".join(w[4] for w in line["words"]))]
                    panel["note_rows"] = _table_rows(lines, headers, note_panel, page,
                                                       include_unlabelled=True)
                    for row in panel["note_rows"]:
                        row["note_title"] = next((title for y, title in reversed(titles) if y < row["y"]), "")
                    panel["note_headers"] = [{k: v for k, v in h.items() if k != "y"} for h in headers]
                    page["tables"] += "\n".join(row["quote"] for row in panel["note_rows"]) + "\n"
    warnings = []
    for (filename, _), units in units_by_document.items():
        if len(units) > 1:
            described = "; ".join(
                f"{currency} x{scale:,} on page(s) {', '.join(map(str, sorted(set(pages_))))}"
                for (currency, scale), pages_ in units.items())
            warnings.append(
                f"{filename}: primary statements declare different units ({described}). "
                "Check each statement heading before using these figures together.")
    return warnings


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
    warnings.extend(_annotate_statements(pages))
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
