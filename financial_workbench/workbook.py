"""Populate allowlisted inputs only; keep the user's formulas, styles and charts."""

from decimal import Decimal
from io import BytesIO
from pathlib import Path

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties

from .engine import CATALOG, METRICS


def text(cell, value):
    # Force text type: document strings must never turn into Excel formulas.
    cell.value = str(value)[:32767]
    cell.data_type = "s"


def export_workbook(settings, decisions, checks, reviewed=True, coverage=(), scenarios=None):
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
        ("Only reviewed inputs are populated. Missing data remains blank. See Export Review."
         if reviewed else "DRAFT: extracted figures are unreviewed. Conflicts and missing inputs remain blank. See Export Review.")
        if language == "en" else
        ("Συμπληρώνονται μόνο ελεγμένα στοιχεία. Τα ελλείποντα παραμένουν κενά. Δείτε Έλεγχος Εξαγωγής."
         if reviewed else "ΠΡΟΣΧΕΔΙΟ: τα στοιχεία δεν έχουν ελεγχθεί. Οι ασυμφωνίες και τα ελλείποντα παραμένουν κενά."),
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
                ("Manual" if d and d.get("manual") else ("Reviewed" if reviewed else "Unreviewed") if d else "Missing")
                if language == "en"
                else (
                    "Χειροκίνητο"
                    if d and d.get("manual")
                    else ("Ελέγχθηκε" if reviewed else "Μη ελεγμένο")
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
    if coverage:
        _add_evidence_kpis(wb, settings, coverage, reviewed)
    if scenarios:
        _add_scenarios(wb, scenarios, settings.language)
    # Excel/LibreOffice calculate the original formulas on opening. openpyxl does not evaluate formulas.
    wb.calculation = CalcProperties(
        calcId=0, fullCalcOnLoad=True, forceFullCalc=True, calcMode="auto"
    )
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _add_evidence_kpis(wb, settings, coverage, reviewed):
    """Transparent ratio formulas with cited operands, separate from the template's definitions."""
    el = settings.language == "el"
    sheet = wb.create_sheet("Τεκμηριωμένοι Δείκτες" if el else "Evidence KPIs")
    sheet.append(["Έτος" if el else "Year", "Δείκτης" if el else "KPI",
                  "Τύπος Excel" if el else "Excel result", "Κατάσταση" if el else "Status",
                  "Ορισμός" if el else "Definition", "Ελλείποντα" if el else "Missing dependencies",
                  "Πηγές / ενδιάμεσα" if el else "Sources and operands"] +
                 [f"{('Είσοδος' if el else 'Input')} {i}" for i in range(1, 17)])
    for item in coverage:
        row = sheet.max_row + 1
        inputs = item["inputs"]
        sources = []
        for index, operand in enumerate(inputs, 8):
            cell = sheet.cell(row, index, float(Decimal(operand["value"])))
            refs = [f"{s.get('row_label', '')}: {s.get('file', '')} p.{s.get('page', '')}"
                    for s in operand["sources"]]
            note = f"{operand['metric_id']} · {operand['year']} · {'; '.join(refs)}"
            cell.comment = Comment(note, "Financial Workbench")
            sources.append(f"{operand['metric_id']} {operand['year']}: {operand['value']} · {'; '.join(refs)}")
        cell = sheet.cell(row, 3)
        if item["status"] == "available":
            # Input cells contain only source values vetted in the review flow.
            x = [f"{get_column_letter(i)}{row}" for i in range(8, 8 + len(inputs))]
            expressions = {
                "quick_liquid": lambda: f"({x[0]}+{x[1]}+{x[2]})/{x[3]}",
                "quick_ex_inventory": lambda: f"({x[0]}-{x[1]})/{x[2]}",
                "ebitda": lambda: f"{x[0]}+{x[1]}",
                "interest_coverage": lambda: f"{x[0]}/{x[1]}",
                "ebitda_coverage": lambda: f"({x[0]}+{x[1]})/{x[2]}",
                "net_debt": lambda: f"SUM({','.join(x[:4])})-{x[4]}-{x[5]}",
                "roic": lambda: (f"{x[0]}*(1-{x[1]})/((SUM({','.join(x[2:6])})+{x[6]}-{x[7]}-{x[8]}+"
                                 f"SUM({','.join(x[9:13])})+{x[13]}-{x[14]}-{x[15]})/2)"),
                "pe": lambda: f"{x[0]}/{x[1]}",
                "reported_net_debt_ebitda": lambda: f"{x[0]}/{x[1]}",
            }
            cell.value = "=" + expressions[item["id"]]()
            cell.number_format = '#,##0.0000;[Red](#,##0.0000)'
        else:
            text(cell, "n.a.")
        sheet.cell(row, 1, item["year"])
        text(sheet.cell(row, 2), item["label"])
        text(sheet.cell(row, 4), (("Μη ελεγμένο" if el else "Unreviewed") if
                                 item["status"] == "available" and not reviewed else item["status"]))
        text(sheet.cell(row, 5), item["formula"])
        text(sheet.cell(row, 6), "; ".join(f"{d['label']} ({d['year']}): {d['reason']}"
                                                for d in item["missing"] + item["ambiguous"]))
        text(sheet.cell(row, 7), "\n".join(sources))
        sheet.row_dimensions[row].height = 46
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = sheet.dimensions
    for col, width in {"A": 10, "B": 53, "C": 19, "D": 19, "E": 88, "F": 75, "G": 95}.items():
        sheet.column_dimensions[col].width = width
    for col in range(8, 24):
        sheet.column_dimensions[get_column_letter(col)].width = 16
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(color="FFFFFF", bold=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row[:7]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def _add_scenarios(wb, scenarios, language):
    el = language == "el"
    sheet = wb.create_sheet("Σενάρια" if el else "Scenarios")
    text(sheet["A1"], "Ενδεικτικά σενάρια εσόδων και EBIT" if el else "Illustrative revenue and EBIT scenarios")
    text(sheet["A2"], "Ιστορική προβολή ή ρητή παραδοχή αναλυτή. Δεν είναι πρόβλεψη τιμής μετοχής."
         if el else "Historical projection or explicit analyst assumption; no stock-price prediction.")
    if scenarios["status"] != "available":
        text(sheet["A4"], scenarios.get("reason", "Insufficient history"))
        sheet.column_dimensions["A"].width = 95
        return
    headings = (("Σενάριο", "Έτος", "Βασική ανάπτυξη", "Τελευταία έσοδα",
                 "Λειτουργικό περιθώριο", "Εύρος (ποσοστιαίες μονάδες)") if el else
                ("Scenario", "Year", "Baseline growth", "Last revenue",
                 "Operating margin", "Spread (percentage points)"))
    for index, heading in enumerate(headings, 1):
        text(sheet.cell(3, index), heading)
    sheet["B4"] = scenarios["latest_year"]
    sheet["C4"] = float(Decimal(scenarios["baseline_growth"]))
    sheet["D4"] = float(Decimal(scenarios["history"][-1]["revenue"]))
    if scenarios["operating_margin"] is not None:
        sheet["E4"] = float(Decimal(scenarios["operating_margin"]))
    sheet["F4"] = float(Decimal(scenarios["shock_pp"]) / 100)
    text(sheet["A4"], ("Παραδοχή αναλυτή" if el else "Analyst") if scenarios["assumption_source"] == "analyst"
         else ("Ιστορική διάμεσος" if el else "Historical median"))
    output_headings = (("Σενάριο", "Οικονομικό έτος", "Ετήσια ανάπτυξη", "Έσοδα",
                        "Λειτουργικά κέρδη (EBIT)", "Μέθοδος") if el else
                       ("Scenario", "Fiscal year", "Annual growth", "Revenue",
                        "Operating profit (EBIT)", "Method"))
    for column, heading in enumerate(output_headings, 1):
        text(sheet.cell(6, column), heading)
    for item in scenarios["scenario_rows"]:
        row = sheet.max_row + 1
        text(sheet.cell(row, 1), ({"downside": "Δυσμενές", "base": "Βασικό", "upside": "Ευνοϊκό"}[item["name"]]
             if el else item["name"].title()))
        sheet.cell(row, 2, item["year"])
        adjustment = {"downside": "-$F$4", "base": "", "upside": "+$F$4"}[item["name"]]
        sheet.cell(row, 3, "=$C$4" + adjustment)
        sheet.cell(row, 4, f"=$D$4*(1+C{row})^(B{row}-$B$4)")
        sheet.cell(row, 5, f'=IF(ISNUMBER($E$4),D{row}*$E$4,"n.a.")')
        text(sheet.cell(row, 6), ("Σταθερό τελευταίο λειτουργικό περιθώριο" if el else "Constant latest operating margin")
             if scenarios["operating_margin"] is not None else
             ("Το EBIT απαιτεί τεκμηριωμένα λειτουργικά κέρδη" if el else "EBIT requires sourced operating profit"))
    base = sheet.max_row + 3
    text(sheet.cell(base, 1), "Ιστορικά στοιχεία και σελίδες πρωτότυπου PDF" if el
         else "Historical inputs and original PDF pages")
    for index, item in enumerate(scenarios["history"], base + 1):
        sheet.cell(index, 1, item["year"])
        sheet.cell(index, 2, float(Decimal(item["revenue"])))
        text(sheet.cell(index, 3), f"{item.get('file', '')} p.{item.get('page', '')}")
        text(sheet.cell(index, 4), item.get("document_id") or "")
    sheet.freeze_panes = "C7"
    for column, width in {"A": 24, "B": 18, "C": 48, "D": 80, "E": 32, "F": 53}.items():
        sheet.column_dimensions[column].width = width
    for row in (3, 6):
        for cell in sheet[row]:
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.font = Font(color="FFFFFF", bold=True)
