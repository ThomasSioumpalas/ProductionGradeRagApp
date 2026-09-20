"""Populate allowlisted inputs only; keep the user's formulas, styles and charts."""

from decimal import Decimal
from io import BytesIO
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.workbook.properties import CalcProperties

from .engine import CATALOG, METRICS


def text(cell, value):
    # Force text type: document strings must never turn into Excel formulas.
    cell.value = str(value)[:32767]
    cell.data_type = "s"


def export_workbook(settings, decisions, checks):
    language = settings.language
    wb = openpyxl.load_workbook(
        Path(__file__).parent / "templates" / f"{language}.xlsx"
    )
    setup = wb["Setup" if language == "en" else "Ρυθμίσεις"]
    for coordinate, value in {
        "E6": settings.company,
        "E9": settings.currency,
        "E15": ("Consolidated" if settings.scope == "consolidated" else "Standalone")
        if language == "en"
        else ("Ενοποιημένα" if settings.scope == "consolidated" else "Ατομικά"),
    }.items():
        text(setup[coordinate], value)
    for coordinate, value in {
        "E10": settings.money_scale,
        "E11": settings.share_scale,
        "E12": settings.latest_year,
    }.items():
        setup[coordinate] = value
    evidence = {}
    for d in decisions:
        m = METRICS[d["metric_id"]]
        sheet, row = m["sheet"][language], m["row"]
        col = d["year"] - settings.latest_year + 9
        if not 4 <= col <= 9:
            raise ValueError("Year outside template window")
        cell = wb[sheet].cell(row, col)
        if cell.data_type == "f" or cell.fill.fgColor.rgb != "FFFFF2CC":
            raise ValueError("Attempt to overwrite a non-input cell")
        cell.value = float(Decimal(d["value"]))
        evidence.setdefault((sheet, row), []).append(d)
    for (sheet, row), items in evidence.items():
        text(
            wb[sheet].cell(row, 12),
            "\n".join(
                f"{d['year']}: {d.get('file', 'Manual / Χειροκίνητο')} p.{d.get('page', '—')}"
                for d in items
            ),
        )
        text(
            wb[sheet].cell(row, 13),
            "\n".join(
                f"{d['year']}: {d.get('quote', d.get('note', ''))} | {d.get('context_quote', '')}"
                for d in items
            ),
        )
        wb[sheet].cell(row, 12).alignment = Alignment(wrap_text=True, vertical="top")
        wb[sheet].cell(row, 13).alignment = Alignment(wrap_text=True, vertical="top")
    overview = wb.worksheets[0]
    text(
        overview["K7"],
        "Only reviewed inputs are populated. Missing data remains blank. See Export Review."
        if language == "en"
        else "Συμπληρώνονται μόνο ελεγμένα στοιχεία. Τα ελλείποντα παραμένουν κενά. Δείτε Έλεγχος Εξαγωγής.",
    )
    audit = wb.create_sheet("Export Review" if language == "en" else "Έλεγχος Εξαγωγής")
    headers = (
        [
            "Metric",
            "Year",
            "Value",
            "Status",
            "Document",
            "PDF page",
            "Quote / manual note",
            "Context",
            "Document SHA256",
        ]
        if language == "en"
        else [
            "Μέγεθος",
            "Έτος",
            "Τιμή",
            "Κατάσταση",
            "Έγγραφο",
            "Σελίδα PDF",
            "Απόσπασμα / σημείωση",
            "Πλαίσιο",
            "SHA256 εγγράφου",
        ]
    )
    for i, v in enumerate(headers, 1):
        text(audit.cell(1, i), v)
    chosen = {(d["metric_id"], d["year"]): d for d in decisions}
    for m in CATALOG:
        for year in range(settings.latest_year - 5, settings.latest_year + 1):
            d = chosen.get((m["id"], year))
            r = audit.max_row + 1
            status = (
                ("Manual" if d and d.get("manual") else "Reviewed" if d else "Missing")
                if language == "en"
                else (
                    "Χειροκίνητο"
                    if d and d.get("manual")
                    else "Ελέγχθηκε"
                    if d
                    else "Λείπει"
                )
            )
            vals = [
                m["label"][language],
                year,
                float(Decimal(d["value"])) if d else None,
                status,
                d.get("file", "") if d else "",
                d.get("page", "") if d else "",
                d.get("quote", d.get("note", "")) if d else "",
                d.get("context_quote", "") if d else "",
                d.get("document_id", "") if d else "",
            ]
            for c, v in enumerate(vals, 1):
                if isinstance(v, (int, float)) or v is None:
                    audit.cell(r, c, v)
                else:
                    text(audit.cell(r, c), v)
    audit.freeze_panes = "C2"
    audit.auto_filter.ref = audit.dimensions
    for c, width in {
        "A": 50,
        "B": 10,
        "C": 18,
        "D": 16,
        "E": 30,
        "F": 12,
        "G": 75,
        "H": 65,
        "I": 68,
    }.items():
        audit.column_dimensions[c].width = width
    for cell in audit[1]:
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(color="FFFFFF", bold=True)
    for row in audit.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
        row[2].number_format = "#,##0.00;[Red](#,##0.00)"
        audit.row_dimensions[row[0].row].height = 42
    # Excel/LibreOffice calculate the original formulas on opening. openpyxl does not evaluate formulas.
    wb.calculation = CalcProperties(
        calcId=0, fullCalcOnLoad=True, forceFullCalc=True, calcMode="auto"
    )
    output = BytesIO()
    wb.save(output)
    return output.getvalue()
