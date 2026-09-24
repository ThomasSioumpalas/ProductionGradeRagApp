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

1. Select English or Greek. Enter the company label you want displayed in Excel, plus the latest fiscal year, reporting scope, currency and output scales. The label is not used to accept or reject PDF facts.
2. Upload annual PDFs for one company and matching reporting scope. Up to 10 PDFs, 40 MB each, 100 MB total, 600 pages per PDF and 1,000 pages per set are supported.
3. Wait for extraction. The service searches **all pages** for statement tables, including later sections and both sides of two-page landscape spreads. It reads dated Group/Company columns and copies numeric tokens from the PDF. Exact, unambiguous labels are mapped locally; Groq receives only unfamiliar **row labels and sections**, never amounts to generate. The counter shows these small label-mapping requests. The full document remains searchable after extraction, including on a failed job. Scanned/image-only documents need OCR first.
4. Open **What the app read** to inspect each recognized panel: original text, exact row labels, four-column values, dates, scope, currency and units. Search terms across all PDFs and open the cited page. Review every candidate against this evidence. Printed text alone does **not** prove that the accounting concept matches your workbook; Groq's label mapping and layouts outside the recognized statements need manual checking.
5. Select one candidate for each metric/year, leave it blank, or enter a manual value with a source/assumption note. Bulk selection only selects metrics with a single distinct value; conflicts require an explicit selection. Restatements are not silently preferred over earlier reports.
6. Save the review. Inspect reconciliation differences and missing-data notices. A mismatch is visible but does not prevent exporting a workbook for further analysis. Ask questions over the persistent job-specific search index; answers cite retrieved pages and should be checked against the PDF.
7. Download either language using the language toggle. Open in Excel or LibreOffice to calculate the retained formulas. Download the JSON evidence file when you need the full machine-readable record.

Manual amounts use the selected **output** scale. Example: money scale 1,000,000 means an input of `120` represents EUR 120 million. Share counts use the separate share scale. EPS, dividends per share and prices stay in currency units per share. Rates are fractions: `0.25` means 25%.

## What is populated

Both original 24-sheet templates are bundled unchanged as `financial_workbench/templates/en.xlsx` and `el.xlsx`. A catalog maps 132 rows across Balance Sheet, Income Statement, Cash Flow Statement, Changes in Equity and Market Inputs to stable IDs. The five analysis years occupy E:I; D holds the preceding year. Populating an input lets the existing workbook formulas calculate profitability, liquidity, growth, cash-flow, solvency, DuPont and valuation measures where their prerequisites exist.

The exporter writes only allowlisted yellow inputs, setup values and source fields. It retains formulas, charts and formatting, adds a localized Export Review sheet, and records document/page citations alongside the input rows. The review sheet lists **every metric/year**, including missing inputs. Document hashes identify the PDF used for each extracted figure. Raw excerpts remain in their source language.

**A full workbook does not mean every cell can be filled from annual reports.** Market prices, valuation multiples, WACC, normalised tax rates, margin-of-safety assumptions, peer companies, personal investments, ETF scenarios and industry-specific inputs may require separate sources or analyst judgment. Those are never guessed. Peer/ETF/personal-investment and industry tabs remain in the template for manual work. Missing inputs stay blank, confirmed zero stays numeric zero, and dependent formulas retain the template's `n.a.` behavior.

No FX conversion or consolidation is performed. Currency and scope mismatches are rejected. Currently the table parser expects selectable text, explicit currency/scale, dated year columns, and printed Group/Company headings; tables with a different layout, equity movement columns, and standalone share counts may need manual review. It will report unreadable panels instead of silently guessing. The generic corporate model may need adaptation for banks and insurers.

## How the code connects

- `documents.py` reads every PDF page with PyMuPDF and splits detected landscape spreads at their empty central gutter. It recognizes annual statement headings, currency/scale, Group/Company year headers, and exact numeric tokens aligned to those columns. It groups rows in batches of at most 16. It preserves full page and panel text. The older page chunk helpers remain for other callers but are not used for workbook extraction.
- `llm.py` calls Groq using strict JSON-schema output, low reasoning effort, bounded retries and a configurable supported model. The default is `openai/gpt-oss-120b`. If that model fails Groq's server-side schema generation, it retries once without provider formatting; Pydantic still validates the identical local schema before any mapping is accepted. Incomplete mappings split into smaller row batches; rate-limit retries honor the provider's stated window.
- `models.py` validates extraction and review payloads. Structured output ensures shape, not factual accuracy.
- `engine.py` maps exact printed labels locally, asks Groq to classify only unfamiliar labels against an allowlist, copies values/year/scope/unit from PDF columns, verifies verbatim excerpts and numeric tokens, parses English/Greek numeric separators using `Decimal`, normalizes units and the template's expense signs, and exposes conflicts. It also performs selected independent reconciliation checks.
- `store.py` persists jobs and extracted pages in SQLite and indexes every page panel and recognized table row using job-specific SQLite FTS5 search. Indexes survive restarts and can be rebuilt for older saved jobs. Uploaded PDFs are stored under generated job IDs; filenames are never used as storage paths.
- `api.py` provides upload, status, review, PDF inspection, evidence, bilingual export, question-answering, retry and deletion endpoints.
- `workbook.py` fills the actual supplied template. It forces external strings to text to avoid spreadsheet formula injection and requests recalculation on opening. `openpyxl` does **not** evaluate Excel formulas; cached computed results are not generated by the server.
- `frontend/src` provides the bilingual React review workflow and filters for year, missing inputs and conflicts.

Questions use job-isolated SQLite FTS5 retrieval over the entire uploaded PDF, plus relevant reviewed inputs, with source references. Each retrieved excerpt includes text around the matching terms, even late in a long page. This does not search the legacy global Qdrant collection. The original multilingual embedding/Qdrant pipeline remains in `main.py` and `data_loader.py`; the two ingestion stores are not synchronized. Keyword retrieval works best with terms present in the PDF; cross-language or broad narrative questions may miss relevant pages. A semantic retrieval layer is a possible extension once its coverage can be tested against real reports.

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

Backend tests cover numeric locale parsing, column-aware parsing, source grounding, model mapping boundaries, search persistence and isolation, long-page retrieval, expense signs, blank/zero distinctions, template formula/chart preservation in both languages, authentication, upload/review/export and reconciliation checks. To additionally verify the 187-page reference report locally without committing its PDF, set `WORKBENCH_REPORT_PDF` to its path when running pytest. That integration test checks the statement positions and printed 2025 revenue, assets, cash flows and tax amounts. No live Groq requests are made by the tests; label classification on unfamiliar reports still needs review against actual sources.

The original template formulas are preserved, not replaced by the legacy `kpis.py` calculations. Recalculation behavior must be checked in the user's Excel/LibreOffice environment; preserving formula text is not a native-engine recalculation test.

## Dependencies and references

`requirements-workbench.lock` pins Python runtime dependencies. `frontend/package-lock.json` pins the frontend dependency tree. To refresh Python dependencies deliberately: `uv pip compile --universal requirements-workbench.txt -o requirements-workbench.lock`, then run the tests.

Implementation references checked September 2026:

- [Groq strict structured outputs](https://console.groq.com/docs/structured-outputs)
- [FastAPI files and forms](https://fastapi.tiangolo.com/tutorial/request-forms-and-files/)
- [React versions](https://react.dev/versions)

Changing `GROQ_EXTRACTION_MODEL` requires a model that supports strict JSON-schema output; the old Llama model used by the legacy Q&A endpoint is not assumed to support that mode.
