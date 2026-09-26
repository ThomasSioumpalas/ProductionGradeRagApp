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

## Publish on the internet (HTTPS)

`Dockerfile.public` builds one container that serves the interface and the API from the same address; `render.yaml` deploys it on [Render](https://render.com) with automatic HTTPS.

1. Sign in to Render with GitHub and allow access to this repository.
2. **New → Blueprint**, choose this repository. Render reads `render.yaml` and creates the `financial-workbench` web service from `feat/bilingual-financial-workbooks`.
3. When asked, paste your `GROQ_API_KEY` (stored by Render, never in the repository). Leave the other values or lower the limits.
4. After the first build the site is live at `https://financial-workbench.onrender.com` (or the name Render assigns). Every push to the branch redeploys it. A custom domain can be added in the service's **Settings → Custom Domains**; Render issues its certificate.
5. To appear in Google results, add the address in [Google Search Console](https://search.google.com/search-console) and request indexing. `robots.txt` allows the site and excludes `/api/`.

With `WORKBENCH_PUBLIC=true` (set by the image):

- Each browser receives an anonymous, random, HttpOnly cookie; only its hash is stored. Visitors see only their own analyses; another visitor's job answers "not found". Clearing cookies loses access.
- Limits (all configurable in `render.yaml`): one running analysis and 5 uploads per browser per day, 50 uploads per day in total, 10 queued jobs, 20 questions and 10 rechecks per browser per day, and 300 Groq requests per day for the whole site. When the Groq allowance is spent, or Groq asks to wait more than 30 seconds, extraction continues without it: reconciled statement figures are unaffected and a warning explains that lines only a model could classify stay in "other" rows. Questions then return a clear message.
- Analyses and their PDFs are deleted 24 hours after their last change.
- The page shows a notice explaining this, that labels (never amounts) may be sent to Groq, and that figures are drafts to verify, not investment advice.

Render's **free** plan sleeps after 15 minutes without visits (the next visit takes about a minute to wake) and its disk is temporary, so saved analyses can disappear on a restart or redeploy; users should download their workbook. A 306-page report needed about 160 MB of memory and 15 seconds on a normal CPU in testing; the free plan's slower CPU takes longer. For persistence, choose a paid plan and attach a disk mounted at `/data`. The same image runs on other Docker hosts that set `$PORT`.

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
5. **Download draft Excel** immediately after extraction to inspect a populated workbook. It contains one cited candidate per metric/year only when all reported amounts agree. Conflicting amounts and missing inputs stay blank; its Export Review rows say **Unreviewed** and its Setup sheet says **DRAFT**. The draft does not save review decisions or change the job status.
6. Select one candidate for each metric/year you have checked, leave it blank, or enter a manual value with a source/assumption note. Bulk selection only selects metrics with a single distinct value; conflicts require an explicit selection. Restatements are not silently preferred over earlier reports. Saving with no selected inputs is rejected, so an empty reviewed workbook cannot be exported.
7. Save the review. Inspect reconciliation differences and missing-data notices. A mismatch is visible but does not prevent exporting a workbook for further analysis. Ask questions over the persistent job-specific search index; answers cite retrieved pages and should be checked against the PDF.
8. Download the reviewed Excel in either language using the language toggle. Open in Excel or LibreOffice to calculate the retained formulas. Download the JSON evidence file when you need the full machine-readable record. JSON `candidates` are extracted possibilities; JSON `decisions` are the values written to a reviewed workbook. Both downloads include the same eight-character job suffix in their filenames so you can match an Excel export to its JSON.

**Recheck source evidence** processes the PDFs already saved for a finished or failed job. It rebuilds the page search index, parses dated note tables and directors' disclosures and adds independently checked figures without contacting Groq. It retains saved selections; review any new candidates and download a fresh workbook. If an older Groq mapping described an individual component as a total, the unselected candidate is removed and logged under rejected mappings. Existing saved selections are retained for manual review.

Manual amounts use the selected **output** scale. Example: money scale 1,000,000 means an input of `120` represents EUR 120 million. Share counts use the separate share scale. EPS, dividends per share and prices stay in currency units per share. Rates are fractions: `0.25` means 25%.

## What is populated

Both original 24-sheet templates are bundled unchanged as `financial_workbench/templates/en.xlsx` and `el.xlsx`. A catalog maps 132 rows across Balance Sheet, Income Statement, Cash Flow Statement, Changes in Equity and Market Inputs to stable IDs. The five analysis years occupy E:I; D holds the preceding year. Populating an input lets the existing workbook formulas calculate profitability, liquidity, growth, cash-flow, solvency, DuPont and valuation measures where their prerequisites exist.

### Complete, reconciled statements

The template's own **Checks** sheet requires every component row of a section (for example all nine current-asset rows) before it compares them with the printed total, so filling only the lines that match a template label leaves most checks and many KPIs at `n.a.`. `statements.py` therefore reads each primary statement as a whole:

- Every printed line of a balance-sheet section, income-statement segment, cash-flow section or statement of changes in equity is assigned to exactly one template row: a specific concept (inventories, finance costs, dividends paid) or that section's residual row (other current assets, other operating income and gains, other non-cash adjustments, other changes in equity). Position matters: a gain above operating profit is operating, the same wording below it is non-operating; an adjustment before working-capital changes is a non-cash adjustment, after it an other operating cash flow.
- Amounts are summed and exported **only when the section's lines add up to the statement's own printed total** (exactly for statements in euros; within 2 printed units for statements in thousands or millions). Each figure lists its component lines, pages and the reconciliation. A section that does not tie contributes no residual and no zero; its individually matched lines still appear as before, and the mismatch is listed in `statement_checks`.
- A template row that no printed line supplies is exported as **0 only inside a reconciled section, and only when no residual line in that section could plausibly be that item**; its citation reads "Not presented as a separate line". A printed dash within a reconciled section is nil. Outside reconciled sections, missing stays blank.
- A combined line such as *Trade and other receivables* is split only with a note whose own lines tie to its printed total and whose total equals the statement line (current, or current plus non-current). Because notes rarely split trade balances by maturity, the trade amount is labelled "assumed current".
- Unprinted totals are derived from printed ones only where the section reconciles (non-current assets = total assets − current assets). Parent equity, profit attribution (the owners / non-controlling pair that sums to profit, not the pair that sums to comprehensive income) and earnings per share (per-share, unscaled) are identified the same way.
- An itemised finance-cost note that separates interest from letters-of-credit fees, discounting and FX is preferred to a cash-flow add-back merely labelled "interest expense"; the superseded line stays on the fact as `superseded`.
- Lines settled by rule inside a reconciled section are not sent to Groq, which roughly halves label-mapping requests on the tested reports. Groq may still move a residual line to a specific row, subject to the local label rules.

Figures that an annual report does not contain remain blank: market prices and indices, analyst assumptions (normalised tax, WACC, fair multiples, maintenance capex), optional purchases/credit sales/past-due liabilities, and years before the two comparative columns unless earlier reports are uploaded. Year-end shares outstanding stays blank because it must exclude treasury shares.

The exporter writes only allowlisted yellow inputs, setup values and source fields. It retains formulas, charts and formatting, adds a localized Export Review sheet, and records document/page citations alongside the input rows. The review sheet lists **every metric/year**, including missing inputs. Document hashes identify the PDF used for each extracted figure. Raw excerpts remain in their source language.

The **Evidence KPIs / Τεκμηριωμένοι Δείκτες** sheet lists actual Excel formulas with cited operands and explains missing dependencies. The sheet uses unreviewed, unconflicted inputs in a draft; the final download uses only selected inputs. Separate rows distinguish the issuer's reported net debt / reported EBITDA from the template's cash-and-liquid-investments net debt / calculated EBITDA. The issuer ratio is only offered when reported net debt reconciles to the selected debt, leases and cash amounts. Likewise, inventory-exclusion quick ratio is shown separately from the liquid-assets definition. A missing liquid-investment amount is never guessed as zero; it is zero only when the current-asset lines reconcile to their printed total without one (see above). A normalised operating tax rate remains an explicit analyst input. The UI displays the same year-specific coverage, formula and document pages before export.

**A full workbook does not mean every cell can be filled from annual reports.** Market prices, valuation multiples, WACC, normalised tax rates, margin-of-safety assumptions, peer companies, personal investments, ETF scenarios and industry-specific inputs may require separate sources or analyst judgment. Those are never guessed. Peer/ETF/personal-investment and industry tabs remain in the template for manual work. Missing inputs stay blank, confirmed zero stays numeric zero, and dependent formulas retain the template's `n.a.` behavior.

### Source investigations and operating scenarios

The KPI review lists each dependency and its actual formula. For missing or disputed automatic inputs, the backend searches the **entire job-specific index**, including financial statements, notes and narrative pages near the end of a report. The interface shows matching page leads, search coverage and pages requiring OCR. A lead is **not** an accepted numeric fact: verify the label, year, consolidation scope, currency and scale against the original page. “No supported match after full index search” does **not** prove the issuer omitted the fact; unusual terms, broken PDF layouts and scans may defeat retrieval. Analyst assumptions stay explicitly marked, and unknown investments are never silently set to zero.

The **Scenarios / Σενάρια** worksheet and interface show three illustrative operating paths. The baseline annual revenue growth is the median of consecutive historical rates over up to six years and requires a sourced latest year plus three consecutive nonconflicting revenue figures. An analyst can instead supply an explicit annual rate. The chosen downside/upside spread ranges from 0 to 20 percentage points. Revenue compounds for three years; EBIT is shown only when operating profit is sourced, with the latest margin held constant. Each history item cites a report and page. These formulas have no probabilities and make no security-price forecast. Review business disposals, changed reporting scope and restatements before using the historical rate.

The pipeline uses typed evidence: PDF page and coordinates → dated and scoped statement rows → validated candidates → reviewed choices → deterministic KPI and scenario formulas. Groq maps only unfamiliar financial *labels*: amounts, periods, units and arithmetic never come from its text output. SQLite FTS5 provides persistent retrieval over all job pages for both questions and systematic gap searches. This follows [FinQA's auditable numerical reasoning](https://aclanthology.org/2021.emnlp-main.300/) and uses [SQLite FTS5](https://www.sqlite.org/fts5.html) and [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/page.html#Page.find_tables) as small, inspectable building blocks. [SEC Company Facts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and [ESMA ESEF](https://www.esma.europa.eu/publications-and-data/interactive-single-rulebook/esef) are potential future corroborating sources, but external data is **not** merged into a PDF job without entity, taxonomy, period and restatement reconciliation.

No FX conversion or consolidation is performed. Currency and scope mismatches are rejected. Currently the table parser expects selectable text, explicit currency/scale, dated year columns, and printed Group/Company headings; tables with a different layout, equity movement columns, and standalone share counts may need manual review. It will report unreadable panels instead of silently guessing. The generic corporate model may need adaptation for banks and insurers.

## How the code connects

- `documents.py` reads every PDF page with PyMuPDF and splits detected landscape spreads at their empty central gutter. It recognizes annual statement headings, dated note tables, currency/scale, Group/Company year headers, and exact numeric tokens aligned to those columns. It groups statement rows in batches of at most 16. It preserves full page and panel text. The older page chunk helpers remain for other callers but are not used for workbook extraction.
- `llm.py` calls Groq using strict JSON-schema output, low reasoning effort, bounded retries and a configurable supported model. The default is `openai/gpt-oss-120b`. If that model fails Groq's server-side schema generation, it retries once without provider formatting; Pydantic still validates the identical local schema before any mapping is accepted. Incomplete mappings split into smaller row batches; rate-limit retries honor the provider's stated window.
- `models.py` validates extraction and review payloads. Structured output ensures shape, not factual accuracy.
- `engine.py` maps exact printed labels locally, asks Groq to classify only unfamiliar labels against an allowlist, copies values/year/scope/unit from PDF columns, verifies verbatim excerpts and numeric tokens, parses English/Greek numeric separators using `Decimal`, normalizes units and the template's expense signs, and exposes conflicts. It also performs selected independent reconciliation checks.
- `statements.py` maps complete primary statements (including the total-equity column of the statement of changes in equity) into template rows, gated by printed totals, as described above. Reconciled facts replace line-by-line candidates for the same statement page.
- `analysis.py` retrieves note panels from the job's search index, validates arithmetic against printed subtotals and adds cited receivables, interest, depreciation, share and reported performance inputs. It computes explicitly defined KPIs using `Decimal` and records why a prerequisite is missing or disputed; it does not ask the language model to perform arithmetic.
- `store.py` persists jobs and extracted pages in SQLite and indexes every page panel, statement row and recognized note row using job-specific SQLite FTS5 search. Indexes survive restarts and can be rebuilt for older saved jobs. Uploaded PDFs are stored under generated job IDs; filenames are never used as storage paths.
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

Backend tests cover numeric locale parsing, column-aware parsing, source grounding, model mapping boundaries, search persistence and isolation, long-page retrieval, expense signs, blank/zero distinctions, draft versus reviewed exports, template formula/chart preservation in both languages, authentication, upload/review/export and reconciliation checks. To verify the 187-page reference report locally without committing its PDF, set `WORKBENCH_REPORT_PDF` to its path when running pytest; set `WORKBENCH_AKTOR_PDF` to AKTOR's 306-page FY2025 report as well to run the reconciled-statement regression on both issuers. To reproduce a user evidence export locally, set `WORKBENCH_EVIDENCE_JSON` to its path; with both paths set, tests check the exact recovered figures, report's net-debt bridge, alternate ratio definitions, cited workbook formulas and idempotent, provider-free refresh. No live Groq requests are made by the tests; label classification on unfamiliar reports still needs review against actual sources.

Optional cross-company tests use the original [Unilever 2025 annual report](https://www.unilever.com/investors/annual-report-and-accounts/) and [adidas 2025 consolidated financial statements](https://www.report.adidas-group.com/2025/en/services/downloads.html). Download both to local disk, set `WORKBENCH_UNILEVER_PDF` and `WORKBENCH_ADIDAS_PDF` to their respective paths and run the same pytest command. Tests verify printed revenue and operating profit on their cited pages, statement positions and notes, and scenario formulas, without live Groq calls. Successful parsing of two issuers does not guarantee arbitrary PDF layouts or OCR quality.

The original template formulas are preserved, not replaced by the legacy `kpis.py` calculations. Recalculation behavior must be checked in the user's Excel/LibreOffice environment; preserving formula text is not a native-engine recalculation test.

## Dependencies and references

`requirements-workbench.lock` pins Python runtime dependencies. `frontend/package-lock.json` pins the frontend dependency tree. To refresh Python dependencies deliberately: `uv pip compile --universal requirements-workbench.txt -o requirements-workbench.lock`, then run the tests.

Implementation references checked September 2026:

- [Groq strict structured outputs](https://console.groq.com/docs/structured-outputs)
- [FastAPI files and forms](https://fastapi.tiangolo.com/tutorial/request-forms-and-files/)
- [React versions](https://react.dev/versions)

Changing `GROQ_EXTRACTION_MODEL` requires a model that supports strict JSON-schema output; the old Llama model used by the legacy Q&A endpoint is not assumed to support that mode.
