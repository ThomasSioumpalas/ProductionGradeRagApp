# Financial Workbench

A React/TypeScript interface and Python/FastAPI service that extends this repository's PDF analysis workflow with bilingual, evidence-backed Excel exports. The original `main:app`, Inngest events, Qdrant collection and legacy report endpoint remain available. The workbench runs through `financial_workbench.api:app`, so exporting a workbook does not require downloading an embedding model or starting Qdrant.

## Start with Docker

1. Create a `.env` file in the repository root, or add the settings from `.env.workbench.example` to your existing one.
2. Set `GROQ_API_KEY` to your Groq key. Do not put the key in the frontend.
3. Run:

```bash
docker compose -f compose.workbench.yaml up --build
```

Open **http://localhost:3000**. API docs: **http://localhost:8001/docs**.

The containers publish to localhost. This is a single-user workbench, not a multi-tenant hosted service. If setting `WORKBENCH_API_KEY`, enter that shared access key in the interface's connection panel. It is distinct from your Groq key. The UI keeps it in memory, not browser storage. Public hosting needs per-user authentication, ownership checks, TLS and quotas.

Data survives container restarts in the `workbench_data` Docker volume. Delete an analysis in the UI to remove its PDFs and saved records. `docker compose ... down` retains that volume; `down -v` deletes it.

## Local development (including Windows)

Python 3.12+ and Node 24 are recommended for the pinned toolchain.

```bash
python -m venv .venv-workbench
# Windows PowerShell:
.venv-workbench\Scripts\Activate.ps1
# macOS/Linux instead: source .venv-workbench/bin/activate
python -m pip install -r requirements-workbench.lock
python -m uvicorn financial_workbench.api:app --host 127.0.0.1 --port 8001 --workers 1
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. The Vite proxy sends `/api` requests to port 8001. If PowerShell blocks `npm.ps1`, use `npm.cmd ci` and `npm.cmd run dev`.

Use **one API worker**. The persistent queue and SQLite database are designed for a single local process. On restart, queued jobs continue, while interrupted extraction jobs are marked failed and can be retried. Retry reprocesses the document set and can incur additional provider usage.

## Workflow

1. Select English or Greek. Enter the company name as it appears in the annual report, latest fiscal year, reporting scope, currency and output scales.
2. Upload annual PDFs for one company and matching reporting scope. Up to 10 PDFs, 40 MB each, 100 MB total, 600 pages per PDF and 1,000 pages per set are supported.
3. Wait for extraction. The service splits every PDF page into overlapping, page-aware chunks. It ranks financial chunks, selects a bounded and diverse set for structured extraction, then sends those chunks in small provider batches. All pages stay available for source viewing and questions. The counter shows provider batches, so it advances steadily even for a large report. Scanned/image-only documents need OCR first.
4. Review candidates. Inspect the raw quote, year/scope/unit context and original PDF. A citation proves where the text occurs; it does **not** prove that the model chose the right year or financial concept. The review step is required for that reason.
5. Select one candidate for each metric/year, leave it blank, or enter a manual value with a source/assumption note. Bulk selection only selects metrics with a single distinct value; conflicts require an explicit selection. Restatements are not silently preferred over earlier reports.
6. Save the review. Inspect reconciliation differences and missing-data notices. A mismatch is visible but does not prevent exporting a workbook for further analysis.
7. Download either language using the language toggle. Open in Excel or LibreOffice to calculate the retained formulas. Download the JSON evidence file when you need the full machine-readable record.

Manual amounts use the selected **output** scale. Example: money scale 1,000,000 means an input of `120` represents EUR 120 million. Share counts use the separate share scale. EPS, dividends per share and prices stay in currency units per share. Rates are fractions: `0.25` means 25%.

## What is populated

Both original 24-sheet templates are bundled unchanged as `financial_workbench/templates/en.xlsx` and `el.xlsx`. A catalog maps 132 rows across Balance Sheet, Income Statement, Cash Flow Statement, Changes in Equity and Market Inputs to stable IDs. The five analysis years occupy E:I; D holds the preceding year. Populating an input lets the existing workbook formulas calculate profitability, liquidity, growth, cash-flow, solvency, DuPont and valuation measures where their prerequisites exist.

The exporter writes only allowlisted yellow inputs, setup values and source fields. It retains formulas, charts and formatting, adds a localized Export Review sheet, and records document/page citations alongside the input rows. The review sheet lists **every metric/year**, including missing inputs. Document hashes identify the PDF used for each extracted figure. Raw excerpts remain in their source language.

**A full workbook does not mean every cell can be filled from annual reports.** Market prices, valuation multiples, WACC, normalised tax rates, margin-of-safety assumptions, peer companies, personal investments, ETF scenarios and industry-specific inputs may require separate sources or analyst judgment. Those are never guessed. Peer/ETF/personal-investment and industry tabs remain in the template for manual work. Missing inputs stay blank, confirmed zero stays numeric zero, and dependent formulas retain the template's `n.a.` behavior.

No FX conversion or consolidation is performed. Currency and scope mismatches are rejected. Interim flows are excluded by the extraction instruction and must be checked during review. The generic corporate model may need adaptation for banks and insurers.

## How the code connects

- `documents.py` reads sorted page text with PyMuPDF, then makes overlapping chunks that retain their original PDF page. It ranks financial terms and numeric density, chooses a configurable maximum of 24 diverse chunks, and sends one 2,500-character chunk per bounded request. It does not run expensive geometric table detection on every page. The extraction engine also routes only relevant workbook metric IDs into each request to stay within provider token limits.
- `llm.py` calls Groq using strict JSON-schema output, bounded retries and a configurable supported model. The default is `openai/gpt-oss-120b`. Incomplete or refused responses are rejected.
- `models.py` validates extraction and review payloads. Structured output ensures shape, not factual accuracy.
- `engine.py` maps claims to the catalog, verifies verbatim excerpts and numeric tokens, parses English/Greek numeric separators using `Decimal`, normalizes units and the template's expense signs, and exposes conflicts. It also performs selected independent reconciliation checks.
- `store.py` persists the queue, page text, extraction evidence and review decisions in SQLite. Uploaded PDFs are stored under generated job IDs; filenames are never used as storage paths.
- `api.py` provides upload, status, review, PDF inspection, evidence, bilingual export, question-answering, retry and deletion endpoints.
- `workbook.py` fills the actual supplied template. It forces external strings to text to avoid spreadsheet formula injection and requests recalculation on opening. `openpyxl` does **not** evaluate Excel formulas; cached computed results are not generated by the server.
- `frontend/src` provides the bilingual React review workflow and filters for year, missing inputs and conflicts.

Questions in the new UI use job-isolated BM25 retrieval over the uploaded pages, plus the saved financial inputs, with source references. This deliberately does not search the legacy global Qdrant collection. The original multilingual embedding/Qdrant pipeline remains in `main.py` and `data_loader.py`; the two ingestion stores are not synchronized. BM25 retrieval works best with terms present in the PDF; cross-language or broad narrative questions may miss relevant pages. For a larger document corpus, the next extension is per-job Qdrant collections or mandatory job filters plus hybrid semantic/lexical retrieval.

PDF text and questions are sent to Groq. Uploaded content is treated as untrusted data in prompts, but model output still needs review. There is no automatic OCR, server-side formula calculation, multi-user authorization or production worker cluster in this implementation.

## Validation

```bash
python -m pip install -r requirements-workbench-dev.txt
python -m pytest tests/test_workbench.py -q
cd frontend
npm ci
npm run build
npx playwright install chromium
npm run test:e2e
```

Backend tests cover numeric locale parsing, source grounding, units, conflicts, blank/zero distinctions, template formula/chart preservation in both languages, formula injection, authentication, invalid inputs, upload/review/export and reconciliation checks. Browser tests exercise upload, source inspection, review, Greek downloads, conflict handling, manual zero entry and mobile width. Validated here: 36 backend tests, 2 Chromium browser tests, TypeScript checking and the production build. English desktop, Greek desktop and Greek mobile screenshots were inspected. Docker itself was not available for an image build. Model responses are mocked in automated tests: live Groq extraction quality must be evaluated on representative company reports with a configured key.

The original template formulas are preserved, not replaced by the legacy `kpis.py` calculations. Recalculation behavior must be checked in the user's Excel/LibreOffice environment; preserving formula text is not a native-engine recalculation test.

## Dependencies and references

`requirements-workbench.lock` pins Python runtime dependencies. `frontend/package-lock.json` pins the frontend dependency tree. To refresh Python dependencies deliberately: `uv pip compile --universal requirements-workbench.txt -o requirements-workbench.lock`, then run the tests.

Implementation references checked September 2026:

- [Groq strict structured outputs](https://console.groq.com/docs/structured-outputs)
- [FastAPI files and forms](https://fastapi.tiangolo.com/tutorial/request-forms-and-files/)
- [React versions](https://react.dev/versions)

Changing `GROQ_EXTRACTION_MODEL` requires a model that supports strict JSON-schema output; the old Llama model used by the legacy Q&A endpoint is not assumed to support that mode.
