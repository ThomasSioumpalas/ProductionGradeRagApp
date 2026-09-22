import asyncio
import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import openpyxl
import pymupdf
import pytest
from fastapi.testclient import TestClient

from financial_workbench.api import create_app
from financial_workbench.documents import batch_windows, page_windows, read_pdf, select_extraction_windows
import financial_workbench.engine as engine_module
from financial_workbench.engine import (
    CATALOG,
    METRICS,
    extract,
    extraction_tasks,
    parse_number,
    reconcile,
    validate_fact,
    metrics_for_chunk,
)
from financial_workbench.models import ExtractedFact, Settings
from financial_workbench.workbook import export_workbook


@pytest.fixture
def settings():
    return Settings(company="Example SA", latest_year=2025)


@pytest.fixture
def page():
    return {
        "id": 0,
        "file": "annual.pdf",
        "sha256": "a" * 64,
        "page": 1,
        "text": "Example SA. Consolidated. Annual 2025. EUR thousands. Statement of Profit or Loss.\nRevenue 1,234.5\nCost of sales (234.5)",
        "tables": "",
    }


def fact(**overrides):
    return ExtractedFact(
        **(
            {
                "metric_id": "income_statement_8",
                "year": 2025,
                "scope": "consolidated",
                "currency": "EUR",
                "raw_value": "1,234.5",
                "decimal_separator": ".",
                "scale": 1000,
                "source_file": "annual.pdf",
                "page": 1,
                "quote": "Revenue 1,234.5",
                "context_quote": "Annual 2025. EUR thousands.",
            }
            | overrides
        )
    )


@pytest.mark.parametrize(
    "raw,separator,expected",
    [
        ("1,234.56", ".", "1234.56"),
        ("1.234,56", ",", "1234.56"),
        ("(1.234,56)", ",", "-1234.56"),
        ("0", ".", "0"),
        ("−12,5", ",", "-12.5"),
        ("1 234,5", ",", "1234.5"),
    ],
)
def test_numeric_locales(raw, separator, expected):
    assert parse_number(raw, separator) == Decimal(expected)


@pytest.mark.parametrize(
    "raw", ["11.625.5", "331\\", "NaN", "inf", "=2+2", "1,23", "(-2)"]
)
def test_bad_numbers(raw):
    with pytest.raises(ValueError):
        parse_number(raw, ".")


def test_grounding_and_scale(settings, page):
    settings.company = "Any Excel display label"
    c = validate_fact(fact(), page, settings)
    assert c["value"] == "1.2345"
    assert c["file"] == "annual.pdf"
    assert (
        validate_fact(
            fact(
                metric_id="income_statement_9",
                raw_value="(234.5)",
                quote="Cost of sales (234.5)",
            ),
            page,
            settings,
        )["value"]
        == "0.2345"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"quote": "Revenue 5", "raw_value": "5"},
        {"scope": "standalone"},
        {"currency": "USD"},
        {"year": 2018},
        {"page": 2},
        {"source_file": "another.pdf"},
        {"metric_id": "market_inputs_18"},
        {"raw_value": "234.5"},
    ],
)
def test_reject_unsupported_claim(settings, page, change):
    with pytest.raises(ValueError):
        validate_fact(fact(**change), page, settings)


def test_conflicts_dedup_and_rejection(settings, page, monkeypatch):
    monkeypatch.setattr(
        engine_module,
        "metrics_for_chunk",
        lambda _content: [{"id": "income_statement_8", "label": "Revenue", "unit": "money"}],
    )
    other = {**page, "page": 2, "text": page["text"].replace("1,234.5", "2,234.5")}

    async def fake(messages, structured=False):
        body = json.loads(messages[1]["content"])
        if "income_statement_8" not in {m["id"] for m in body["metrics"]}:
            return json.dumps({"facts": []})
        n = body["pdf_chunks"][0]["page"]
        f = fact(
            page=n,
            raw_value="1,234.5" if n == 1 else "2,234.5",
            quote="Revenue 1,234.5" if n == 1 else "Revenue 2,234.5",
        )
        return json.dumps(
            {
                "facts": [
                    f.model_dump(),
                    f.model_dump(),
                    fact(page=n, raw_value="999", quote="Made up 999").model_dump(),
                ]
            }
        )

    progress = []
    candidates, rejected = asyncio.run(
        extract(
            [page, other], settings, lambda a, b: progress.append((a, b)), complete=fake
        )
    )
    assert len(candidates) == 2 and all(c["status"] == "conflict" for c in candidates)
    assert len(rejected) == 2 and progress[-1] == (2, 2)


def test_incomplete_group_is_split_and_retried(settings, page, monkeypatch):
    from financial_workbench.llm import IncompleteOutputError

    monkeypatch.setattr(
        engine_module,
        "metrics_for_chunk",
        lambda _content: [
            {"id": "income_statement_8", "label": "Revenue", "unit": "money"},
            {"id": "income_statement_9", "label": "Cost of sales", "unit": "money"},
        ],
    )
    calls, progress = [], []

    async def fake(_messages, structured=False):
        calls.append(_messages)
        body = json.loads(_messages[1]["content"])
        if len(body["metrics"]) > 1:
            raise IncompleteOutputError("length", "test output limit")
        return json.dumps({"facts": []})

    candidates, rejected = asyncio.run(
        extract(pages=[page], settings=settings, progress=lambda a, b: progress.append((a, b)), complete=fake)
    )
    assert candidates == [] and rejected == []
    assert len(calls) == 3
    assert progress[-1] == (2, 2)


def test_pdf_and_page_chunks(tmp_path):
    p = tmp_path / "report.pdf"
    d = pymupdf.open()
    d.new_page().insert_text(
        (40, 60),
        "Example SA annual consolidated report 2025. EUR thousands. Statement of Profit or Loss. Revenue 100.",
    )
    d.save(p)
    d.close()
    pages, _warnings = read_pdf(p, "report.pdf")
    assert pages[0]["page"] == 1 and "Revenue 100" in pages[0]["text"]
    windows = list(page_windows([{**pages[0], "text": "x" * 35000}]))
    assert len(windows) == 16 and all(p["page"] == 1 for p, _ in windows)
    blank = tmp_path / "scan.pdf"
    d = pymupdf.open()
    d.new_page()
    d.save(blank)
    d.close()
    with pytest.raises(ValueError, match="OCR"):
        read_pdf(blank, "scan.pdf")


def test_large_pdf_chunks_are_bounded_and_batched():
    pages = [
        {"id": i, "file": "large.pdf", "sha256": "a" * 64, "page": i + 1,
         "text": ("Statement of Financial Position Total assets Revenue " if i in (20, 120) else "Narrative page ") + "1,234 " * 2500,
         "tables": ""}
        for i in range(180)
    ]
    windows = list(page_windows(pages))
    selected = select_extraction_windows(windows, max_chunks=12)
    batches = batch_windows(selected)
    assert len(windows) > 180
    assert len(selected) == 12
    assert {21, 121}.issubset({page["page"] for page, _ in selected})
    assert all(len(batch) <= 1 and sum(len(chunk) for _, chunk in batch) <= 2_500 for batch in batches)


def test_metric_routing_keeps_statement_requests_small():
    metrics = metrics_for_chunk("Statement of Financial Position Total assets cash and cash equivalents")
    assert 1 <= len(metrics) <= 36
    assert all(m["id"].startswith("balance_sheet_") for m in metrics)
    assert {"id", "label", "unit"} == set(metrics[0])
    tasks = extraction_tasks([[({"page": 1}, "Statement of Financial Position")]])
    assert tasks and all(len(task_metrics) <= 4 for _, task_metrics in tasks)


@pytest.mark.parametrize("lang", ["en", "el"])
def test_export_preserves_template_and_blanks(settings, page, lang):
    settings.language = lang
    c = validate_fact(fact(), page, settings)
    d = {**c, "manual": False}
    wb = openpyxl.load_workbook(BytesIO(export_workbook(settings, [d], [])))
    template = openpyxl.load_workbook(
        Path("financial_workbench/templates") / f"{lang}.xlsx"
    )
    assert wb.sheetnames[:24] == template.sheetnames
    s = METRICS["income_statement_8"]["sheet"][lang]
    assert wb[s]["I8"].value == 1.2345 and wb[s]["H8"].value is None
    assert "annual.pdf" in wb[s]["L8"].value
    for original in template:
        actual = wb[original.title]
        assert len(actual._charts) == len(original._charts)
        for row in original:
            for cell in row:
                if cell.data_type == "f":
                    assert actual[cell.coordinate].value == cell.value
    assert wb.calculation.fullCalcOnLoad
    assert len(wb.worksheets[-1]["A"]) == 1 + len(CATALOG) * 6


def test_formula_injection_and_zero(settings):
    settings.company = '=HYPERLINK("evil")'
    decisions = [
        {
            "metric_id": "income_statement_8",
            "year": 2025,
            "value": "0",
            "manual": True,
            "note": "=1+1",
        }
    ]
    wb = openpyxl.load_workbook(BytesIO(export_workbook(settings, decisions, [])))
    assert wb["Setup"]["E6"].data_type == "s"
    assert wb["Income Statement"]["I8"].value == 0
    assert not any(c.data_type == "f" for row in wb["Export Review"] for c in row)


def test_reconciliation_missing_and_mismatch(settings):
    data = [
        {"metric_id": k, "year": 2025, "value": v}
        for k, v in [
            ("balance_sheet_29", "100"),
            ("balance_sheet_45", "60"),
            ("balance_sheet_52", "39"),
        ]
    ]
    checks = reconcile(data, settings)
    c = next(
        c
        for c in checks
        if c["year"] == 2025 and c["check"] == "Assets = liabilities + equity"
    )
    assert c["status"] == "mismatch" and c["difference"] == "1"
    assert any(c["status"] == "missing" for c in checks)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-placeholder")
    monkeypatch.delenv("WORKBENCH_API_KEY", raising=False)
    with TestClient(create_app(tmp_path, start_worker=False)) as c:
        yield c


def upload(client, settings):
    d = pymupdf.open()
    d.new_page().insert_text(
        (40, 60), "Example SA annual consolidated report 2025 EUR thousands Statement of Profit or Loss Revenue 100"
    )
    pdf = d.tobytes()
    d.close()
    response = client.post(
        "/api/jobs",
        data={"settings": settings.model_dump_json()},
        files=[("files", ("report.pdf", pdf, "application/pdf"))],
    )
    assert response.status_code == 202
    return response.json()


def test_api_upload_review_export_persistence(client, settings, page):
    job = upload(client, settings)
    jid = job["id"]
    assert client.get(f"/api/jobs/{jid}/workbook").status_code == 409
    stored = client.app.state.store.get(jid)
    c = validate_fact(fact(), page, settings)
    stored.update(status="review", candidates=[c])
    client.app.state.store.put(stored)
    payload = {
        "decisions": [
            {"metric_id": c["metric_id"], "year": 2025, "candidate_id": c["id"]}
        ]
    }
    assert client.put(f"/api/jobs/{jid}/review", json=payload).status_code == 200
    assert client.get(f"/api/jobs/{jid}").json()["decisions"][0]["value"] == "1.2345"
    for lang in ["en", "el"]:
        response = client.get(f"/api/jobs/{jid}/workbook?language={lang}")
        assert response.status_code == 200
        wb = openpyxl.load_workbook(BytesIO(response.content))
        assert wb[METRICS[c["metric_id"]]["sheet"][lang]]["I8"].value == 1.2345
    assert client.get(f"/api/jobs/{jid}/workbook?language=xx").status_code == 422
    assert client.get(f"/api/jobs/{jid}/audit").status_code == 200
    assert client.delete(f"/api/jobs/{jid}").status_code == 204
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_invalid_upload(client, settings):
    response = client.post(
        "/api/jobs",
        data={"settings": settings.model_dump_json()},
        files=[("files", ("a.pdf", b"bad", "application/pdf"))],
    )
    assert response.status_code == 422
    assert client.get("/api/jobs").json() == []


def test_auth(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_API_KEY", "private")
    assert client.get("/api/jobs").status_code == 401
    assert (
        client.get("/api/catalog", headers={"X-API-Key": "private"}).status_code == 200
    )


def test_invalid_review(client, settings):
    job = upload(client, settings)
    jid = job["id"]
    stored = client.app.state.store.get(jid)
    stored["status"] = "review"
    client.app.state.store.put(stored)
    for d in [
        {"metric_id": "income_statement_8", "year": 2025, "candidate_id": "fake"},
        {"metric_id": "income_statement_8", "year": 2025, "manual_value": "10"},
        {
            "metric_id": "market_inputs_18",
            "year": 2025,
            "manual_value": "25",
            "note": "WACC",
        },
        {
            "metric_id": "income_statement_8",
            "year": 1900,
            "manual_value": "10",
            "note": "report",
        },
        {
            "metric_id": "income_statement_8",
            "year": 2025,
            "manual_value": "NaN",
            "note": "report",
        },
    ]:
        assert (
            client.put(f"/api/jobs/{jid}/review", json={"decisions": [d]}).status_code
            == 422
        )
    d = {
        "metric_id": "market_inputs_18",
        "year": 2025,
        "manual_value": "0.08",
        "note": "Analyst scenario, not reported",
    }
    assert (
        client.put(f"/api/jobs/{jid}/review", json={"decisions": [d]}).status_code
        == 200
    )
    assert (
        client.put(f"/api/jobs/{jid}/review", json={"decisions": [d, d]}).status_code
        == 422
    )


def test_worker_pdf_to_ready_without_network(tmp_path, monkeypatch, settings):
    import time

    import financial_workbench.api as api_module

    monkeypatch.setenv("GROQ_API_KEY", "test-placeholder")
    monkeypatch.delenv("WORKBENCH_API_KEY", raising=False)

    async def fake_complete(messages, structured=False):
        body = json.loads(messages[1]["content"])
        assert "Revenue 100" in body["pdf_chunks"][0]["content"]
        return json.dumps(
            {
                "facts": [
                    fact(
                        raw_value="100",
                        quote="Revenue 100",
                        context_quote="2025 EUR thousands",
                    ).model_dump()
                ]
            }
        )

    async def extraction(pages, config, progress):
        return await extract(pages, config, progress, complete=fake_complete)

    monkeypatch.setattr(api_module, "extract", extraction)
    with TestClient(create_app(tmp_path)) as client:
        job = upload(client, settings)
        for _ in range(100):
            result = client.get(f"/api/jobs/{job['id']}").json()
            if result["status"] in ("review", "failed"):
                break
            time.sleep(0.03)
        assert result["status"] == "review", result.get("error")
        assert result["candidates"][0]["value"] == "0.100"
        c = result["candidates"][0]
        assert client.get(
            f"/api/jobs/{job['id']}/documents/{c['document_id']}"
        ).content.startswith(b"%PDF-")
        assert (
            client.put(
                f"/api/jobs/{job['id']}/review",
                json={
                    "decisions": [
                        {
                            "metric_id": c["metric_id"],
                            "year": 2025,
                            "candidate_id": c["id"],
                        }
                    ]
                },
            ).status_code
            == 200
        )
        wb = openpyxl.load_workbook(
            BytesIO(client.get(f"/api/jobs/{job['id']}/workbook").content)
        )
        assert wb["Income Statement"]["I8"].value == 0.1


def test_question_isolation(client, settings, page, monkeypatch):
    import financial_workbench.api as api_module

    one = upload(client, settings)
    two = upload(client, settings)
    for job, body in [
        (one, page),
        (two, {**page, "text": "Other company confidential SECRET_987 revenue 9900"}),
    ]:
        stored = client.app.state.store.get(job["id"])
        stored.update(status="review", pages=[body])
        client.app.state.store.put(stored)

    async def fake_complete(messages, structured=False):
        assert "SECRET_987" not in json.dumps(messages)
        assert "Greek" in messages[0]["content"]
        return "Έσοδα με αναφορά [1]."

    monkeypatch.setattr(api_module, "completion", fake_complete)
    response = client.post(
        f"/api/jobs/{one['id']}/ask",
        json={"question": "revenue 2025", "language": "el"},
    )
    assert response.status_code == 200
    assert response.json()["sources"][0]["file"] == "annual.pdf"


def test_restart_marks_inflight_failed(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("GROQ_API_KEY", "test-placeholder")
    monkeypatch.delenv("WORKBENCH_API_KEY", raising=False)
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        j = upload(client, settings)
        job = client.app.state.store.get(j["id"])
        job["status"] = "extracting"
        client.app.state.store.put(job)
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        assert client.get(f"/api/jobs/{j['id']}").json()["status"] == "failed"
        assert client.post(f"/api/jobs/{j['id']}/retry").json()["status"] == "queued"


def test_provider_schema_and_incomplete_output(monkeypatch):
    import httpx

    from financial_workbench.llm import ProviderError, completion

    monkeypatch.setenv("GROQ_API_KEY", "test-placeholder")
    original = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length", "message": {"content": '{"facts":'}}
                ]
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ProviderError, match="incomplete"):
        asyncio.run(completion([{"role": "user", "content": "test"}], structured=True))


def test_provider_retry_delay_parses_daily_limit_window():
    from financial_workbench.llm import retry_delay

    class FakeResponse:
        headers = {}

        @staticmethod
        def json():
            return {
                "error": {
                    "message": "Tokens per day limit reached. Please try again in 3m42.047999999s."
                }
            }

    assert retry_delay(FakeResponse(), 0) == pytest.approx(222.048)
