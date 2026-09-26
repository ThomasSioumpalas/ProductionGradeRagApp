"""Map complete primary statements into template rows, gated by printed totals.

Every printed line of a balance-sheet section, income-statement segment or
cash-flow section is assigned to exactly one template row: a specific concept
(inventories, finance costs) or the section's residual row (other current
assets, other non-cash adjustments). Amounts are summed only when the lines
add up to the statement's own printed total, so a residual row cannot hide an
unread or misread line. A template row that no printed line supplies is set to
zero only in a reconciled section, and only when no line in that section could
plausibly be that item. Nothing here asks a language model for a number.
"""

import re
import uuid
from decimal import Decimal

from .engine import _plain_label, _separator, model_mapping_rejection, parse_number

DASHES = ("-", "–", "—")
# Template rows whose inputs are positive expenses (see the Income Statement
# sheet note: "Expenses positive; income, losses and adjustments keep their
# economic sign"). Printed amounts are converted from the report's own sign.
EXPENSE_ROWS = {"income_statement_9", "income_statement_11", "income_statement_12",
                "income_statement_13", "income_statement_14", "income_statement_17",
                "income_statement_21", "income_statement_26"}


def _has(text, *stems):
    """True when a stem starts at a word boundary (stems may end mid-word)."""
    return any(re.search(r"(?<![^\W_])" + re.escape(stem), text) for stem in stems)


# ---------------------------------------------------------------- balance sheet

BALANCE_SECTIONS = {
    "CA": (["balance_sheet_%d" % n for n in range(8, 17)], "balance_sheet_17", "balance_sheet_16"),
    "NCA": (["balance_sheet_%d" % n for n in range(19, 28)], "balance_sheet_28", "balance_sheet_27"),
    "CL": (["balance_sheet_%d" % n for n in range(31, 37)], "balance_sheet_37", "balance_sheet_36"),
    "NCL": (["balance_sheet_%d" % n for n in range(39, 44)], "balance_sheet_44", "balance_sheet_43"),
    "EQ": (["balance_sheet_47", "balance_sheet_48", "balance_sheet_49", "balance_sheet_51",
            "balance_sheet_53"], "balance_sheet_52", "balance_sheet_48"),
}
_BORROWING = ("borrowing", "loan", "bond", "debt", "overdraft", "notes payable",
              "commercial paper", "δανει", "ομολογ")


def _balance_section(section):
    s = _plain_label(section)
    if _has(s, "equity", "ιδια κεφαλαια", "ιδιων κεφαλαιων", "καθαρη θεση"):
        return "EQ"
    non_current = _has(s, "non current", "μη κυκλοφορ", "μακροπροθεσμ")
    if _has(s, "asset", "ενεργητ", "κυκλοφορ"):
        return "NCA" if non_current else "CA"
    if _has(s, "liabilit", "υποχρεωσ", "βραχυπροθεσμ", "μακροπροθεσμ"):
        return "NCL" if non_current else "CL"
    return None


def _balance_total(label):
    if not _has(label, "total", "συνολ"):
        return None
    if _has(label, "equity", "ιδια", "ιδιων") and _has(label, "liabilit", "υποχρεωσ"):
        return "EL"
    if _has(label, "attributable", "owner", "shareholder", "parent", "μετοχ", "ιδιοκτητ"):
        return "balance_sheet_50"
    if _has(label, "equity", "ιδια κεφαλαια", "ιδιων κεφαλαιων"):
        return "balance_sheet_52"
    non_current = _has(label, "non current", "μη κυκλοφορ", "μακροπροθεσμ")
    if _has(label, "asset", "ενεργητ"):
        if _has(label, "current", "κυκλοφορ"):
            return "balance_sheet_28" if non_current else "balance_sheet_17"
        return "balance_sheet_29"
    if _has(label, "liabilit", "υποχρεωσ"):
        if _has(label, "current", "βραχυπροθεσμ") or non_current:
            return "balance_sheet_44" if non_current else "balance_sheet_37"
        return "balance_sheet_45"
    return None


def _balance_item(label, section, scope):
    """Return (metric, rule). Unmatched lines fall to the section's residual row."""
    if section == "EQ":
        if _has(label, "attributable", "owners of", "shareholders of", "equity holders") \
                and _has(label, "equity", "ιδια"):
            return "balance_sheet_50", "subtotal"
        if _has(label, "non controlling", "minority", "μη ελεγχουσ", "μειοψηφ"):
            return "balance_sheet_51", "rule"
        if _has(label, "share capital", "share premium", "paid in", "μετοχικ", "υπερ το αρτιο"):
            return "balance_sheet_47", "rule"
        if _has(label, "retained", "accumulated", "carried forward", "εις νεον"):
            return "balance_sheet_49", "rule"
        if _has(label, "prefer", "προνομιουχ"):
            return "balance_sheet_53", "rule"
        return "balance_sheet_48", "residual"
    if section == "CA":
        if _has(label, "restricted", "committed", "blocked", "pledged", "escrow", "δεσμευμεν"):
            return "balance_sheet_15", "rule"
        if _has(label, "cash", "ταμειακ", "διαθεσιμ"):
            return "balance_sheet_8", "rule"
        if _has(label, "inventor", "αποθεμ"):
            return "balance_sheet_11", "rule"
        if _has(label, "trade", "πελατ", "εμπορικ") and _has(label, "receivable", "debtor", "απαιτησ"):
            return ("combined_receivables", "combined") if _has(label, "other", "λοιπ") \
                else ("balance_sheet_10", "rule")
        if _has(label, "prepa", "προπληρωμ"):
            return "balance_sheet_12", "rule"
        if _has(label, "tax", "φορ") and not _has(label, "deferred", "αναβαλλομεν"):
            return "balance_sheet_13", "rule"
        if _has(label, "derivative", "παραγωγ"):
            return "balance_sheet_14", "rule"
        if _has(label, "fair value through profit", "short term investment", "marketable",
                "treasury bill", "held for trading"):
            return "balance_sheet_9", "rule"
        return "balance_sheet_16", "residual"
    if section == "NCA":
        if _has(label, "goodwill", "υπεραξ"):
            return "balance_sheet_21", "rule"
        if _has(label, "intangible", "ασωματ"):
            return "balance_sheet_22", "rule"
        if _has(label, "right of use", "right to use", "δικαιωμα", "δικαιωματα χρησ"):
            return "balance_sheet_20", "rule"
        if _has(label, "investment property", "επενδυσεις σε ακινητα"):
            return "balance_sheet_27", "rule"
        if _has(label, "property", "plant", "equipment", "tangible", "ενσωματ"):
            return "balance_sheet_19", "rule"
        subsidiaries = _has(label, "subsidiar", "θυγατρ")
        associates = _has(label, "associate", "joint venture", "equity method", "συγγεν", "κοινοπραξ")
        if subsidiaries and associates:
            # Consolidated statements eliminate subsidiaries, so a combined
            # investment line there holds associates and joint ventures.
            return ("balance_sheet_23" if scope == "consolidated" else "balance_sheet_24"), "rule"
        if subsidiaries:
            return "balance_sheet_24", "rule"
        if associates:
            return "balance_sheet_23", "rule"
        if _has(label, "deferred tax", "αναβαλλομεν"):
            return "balance_sheet_26", "rule"
        if _has(label, "financial", "derivative", "receivable", "loan", "securities", "deposit",
                "χρηματοοικονομ", "απαιτησ", "δανει"):
            return "balance_sheet_25", "rule"
        return "balance_sheet_27", "residual"
    if section == "CL":
        if _has(label, "trade", "supplier", "προμηθευτ", "εμπορικ") and _has(
                label, "payable", "creditor", "supplier", "υποχρεωσ", "προμηθευτ"):
            return ("combined_payables", "combined") if _has(label, "other", "λοιπ") \
                else ("balance_sheet_31", "rule")
        if _has(label, "lease", "μισθωσ"):
            return "balance_sheet_33", "rule"
        if _has(label, *_BORROWING):
            return "balance_sheet_32", "rule"
        if _has(label, "tax", "φορ") and not _has(label, "deferred", "αναβαλλομεν"):
            return "balance_sheet_34", "rule"
        if _has(label, "provision", "προβλεψ"):
            return "balance_sheet_35", "rule"
        return "balance_sheet_36", "residual"
    if section == "NCL":
        if _has(label, "lease", "μισθωσ"):
            return "balance_sheet_40", "rule"
        if _has(label, *_BORROWING):
            return "balance_sheet_39", "rule"
        if _has(label, "retirement", "pension", "employee", "severance", "benefit obligation",
                "εργαζομεν", "αποζημιωσ", "συνταξ"):
            return "balance_sheet_41", "rule"
        if _has(label, "deferred tax", "αναβαλλομεν"):
            return "balance_sheet_42", "rule"
        return "balance_sheet_43", "residual"
    return None, "unassigned"


# ------------------------------------------------------------- income statement

def _income_anchor(label):
    if _has(label, "gross", "μικτ"):
        return "income_statement_10"
    if _has(label, "before tax", "before income tax", "before taxes", "pre tax", "προ φορ"):
        return "income_statement_25"
    if (_has(label, "operating", "εκμεταλλευσ", "λειτουργικ") and _has(
            label, "profit", "result", "income", "loss", "κερδ", "αποτελεσμ", "ζημ")
            and not _has(label, "other", "before", "expense", "λοιπ", "εξοδ")) \
            or _has(label, "profit from operations", "ebit"):
        return "income_statement_16"
    return None


def _income_item(label, segment):
    if segment == "S0":
        if _has(label, "cost of", "κοστος"):
            return "income_statement_9", "rule"
        if _has(label, "revenue", "sales", "turnover", "πωλησ", "κυκλος εργασιων", "εσοδα"):
            return "income_statement_8", "rule"
        return None, "unassigned"
    if segment == "S1":
        if _has(label, "selling", "distribution", "marketing", "διαθεσ"):
            return "income_statement_11", "rule"
        if _has(label, "administrat", "general", "διοικητικ"):
            return "income_statement_12", "rule"
        if _has(label, "research", "development", "ερευν"):
            return "income_statement_13", "rule"
        if _has(label, "income", "gain", "εσοδ", "κερδ"):
            return "income_statement_15", "rule"
        if _has(label, "expense", "cost", "loss", "impairment", "provision", "write",
                "εξοδ", "ζημ", "απομειωσ", "προβλεψ"):
            return "income_statement_14", "rule"
        return "income_statement_15", "residual"
    if segment == "S2":
        if _has(label, "net finance", "finance cost net", "finance costs net", "net financial",
                "finance result", "financial result", "καθαρ"):
            return None, "subtotal"
        if _has(label, "associate", "joint venture", "equity method", "συγγεν", "κοινοπραξ"):
            return "income_statement_23", "rule"
        finance = _has(label, "finance", "financial", "interest", "χρηματοοικονομ", "τοκ")
        if finance and _has(label, "cost", "expense", "charge", "εξοδ", "κοστ"):
            return "income_statement_21", "rule"
        if finance and _has(label, "income", "revenue", "εσοδ"):
            return "income_statement_20", "rule"
        return "income_statement_24", "residual"
    if segment == "S3":
        if _has(label, "discontinued", "διακοπεισ"):
            return "income_statement_27", "rule"
        if _has(label, "tax", "φορ"):
            return "income_statement_26", "rule"
        if _has(label, "continuing", "συνεχιζομεν"):
            return None, "subtotal"
        return None, "unassigned"
    return None, "unassigned"


def _is_net_profit(label):
    return (_has(label, "profit", "loss", "result", "earning", "κερδ", "ζημ", "αποτελεσμ")
            and _has(label, "after tax", "net of tax", "for the year", "for the period",
                     "net profit", "net income", "μετα απο φορ", "μετα φορ", "χρησης")
            and not _has(label, "comprehensive", "attributable", "συνολικ"))


# ---------------------------------------------------------------- cash flows

def _cash_section(section):
    s = _plain_label(section)
    if _has(s, "operating", "λειτουργικ"):
        return "OP"
    if _has(s, "investing", "επενδυτικ"):
        return "INV"
    if _has(s, "financing", "χρηματοδοτικ"):
        return "FIN"
    return None


def _cash_net(label):
    if not (_has(label, "net", "καθαρ") or _has(label, "total", "συνολ")):
        return None
    if _has(label, "operating", "λειτουργικ"):
        return "cash_flow_statement_18"
    if _has(label, "investing", "επενδυτικ"):
        return "cash_flow_statement_27"
    if _has(label, "financing", "χρηματοδοτικ"):
        return "cash_flow_statement_37"
    return None


def _cash_item(label, section, after_working_capital, first):
    paid = _has(label, "paid", "καταβλ", "πληρω")
    received = _has(label, "received", "εισπρα", "εισπραχθ")
    if section == "OP":
        if first and _has(label, "profit", "loss", "result", "κερδ", "ζημ", "αποτελεσμ"):
            return "cash_flow_statement_8", "rule"
        # "Cash flows from operating activities before changes in working
        # capital" and "Cash generated from operations" are subtotals.
        if _has(label, "operating activities", "generated from operations",
                "λειτουργικες δραστηριοτητες"):
            return None, "subtotal"
        if _has(label, "depreciation", "amortis", "amortiz", "αποσβεσ") and not _has(
                label, "grant", "επιχορηγ"):
            return "cash_flow_statement_9", "rule"
        if paid and _has(label, "interest", "finance cost", "τοκ", "χρηματοοικονομ"):
            return "cash_flow_statement_15", "rule"
        if paid and _has(label, "tax", "φορ"):
            return "cash_flow_statement_16", "rule"
        if _has(label, "increase", "decrease", "change", "αυξησ", "μειωσ", "μεταβολ"):
            if _has(label, "inventor", "αποθεμ"):
                return "cash_flow_statement_11", "working"
            if _has(label, "receivable", "debtor", "απαιτησ"):
                return "cash_flow_statement_12", "working"
            if _has(label, "payable", "liabilit", "creditor", "υποχρεωσ", "προμηθευτ"):
                return "cash_flow_statement_13", "working"
            return "cash_flow_statement_14", "working"
        return ("cash_flow_statement_17" if after_working_capital
                else "cash_flow_statement_10"), "residual"
    if section == "INV":
        fixed = _has(label, "ppe", "property plant", "property, plant", "plant and equipment",
                     "tangible", "intangible", "fixed asset", "ενσωματ", "ασωματ", "παγι")
        if fixed and not _has(label, "investment property", "grant", "insurance", "επιχορηγ") \
                and _has(label, "purchase", "acquisition", "payment", "addition", "capital expenditure",
                         "αγορ", "αποκτ"):
            return "cash_flow_statement_20", "rule"
        if fixed and not _has(label, "investment property", "grant", "insurance", "επιχορηγ") \
                and _has(label, "proceeds", "disposal", "sale", "πωλησ", "εκποιησ"):
            return "cash_flow_statement_21", "rule"
        if _has(label, "investment property", "επενδυσεις σε ακινητα"):
            return "cash_flow_statement_26", "rule"
        if _has(label, "interest", "τοκ") and received:
            return "cash_flow_statement_24", "rule"
        if _has(label, "dividend", "μερισμ") and received:
            return "cash_flow_statement_25", "rule"
        if _has(label, "subsidiar", "business", "θυγατρ") and _has(
                label, "acquisition", "purchase", "foundation", "contribution", "αποκτ", "αγορ", "ιδρυσ"):
            return "cash_flow_statement_22", "rule"
        if _has(label, "financial asset", "investment", "securities", "associate", "joint venture",
                "subsidiar", "share capital", "επενδυσ", "χρεογραφ", "συγγεν", "θυγατρ"):
            return "cash_flow_statement_23", "rule"
        return "cash_flow_statement_26", "residual"
    if section == "FIN":
        if _has(label, "lease", "μισθωσ"):
            return "cash_flow_statement_33", "rule"
        if _has(label, "treasury", "own shares", "repurchase", "buy back", "buyback", "ιδιες μετοχ"):
            return "cash_flow_statement_30", "rule"
        if _has(label, "dividend", "μερισμ"):
            return "cash_flow_statement_34", "rule"
        if paid and _has(label, "interest", "finance cost", "τοκ", "χρηματοοικονομ"):
            return "cash_flow_statement_35", "rule"
        if _has(label, *_BORROWING) and not _has(label, "expense", "cost", "fee", "εξοδ"):
            if _has(label, "repay", "repaid", "settle", "εξοφλ", "αποπληρ"):
                return "cash_flow_statement_32", "rule"
            if _has(label, "proceeds", "drawdown", "new", "received", "raised", "issued",
                    "εισπραξ", "αναληψ", "εκδοσ"):
                return "cash_flow_statement_31", "rule"
        if (_has(label, "share", "capital", "option", "μετοχ", "κεφαλαι")
                and _has(label, "issue", "increase", "exercise", "proceeds", "εκδοσ", "αυξησ")
                and not _has(label, "non controlling", "minority", "subsidiar", "μη ελεγχ", "θυγατρ")):
            return "cash_flow_statement_29", "rule"
        return "cash_flow_statement_36", "residual"
    return None, "unassigned"


def _cash_bridge(label):
    if _has(label, "beginning", "opening", "start of", "αρχη"):
        return "cash_flow_statement_40"
    if _has(label, "end of", "closing", "end of the", "τελος", "ληξη"):
        return "cash_flow_statement_41"
    if _has(label, "exchange", "currency", "translation", "συναλλαγματ"):
        return "cash_flow_statement_39"
    if _has(label, "net increase", "net decrease", "net change", "καθαρη αυξηση", "καθαρη μειωση"):
        return "subtotal"
    return "other"


# ------------------------------------------------------------------ plumbing

class _Row:
    """One printed line with its scoped, dated amounts in printed units."""

    def __init__(self, row, settings):
        self.row = row
        self.label = _plain_label(row["label"])
        self.amounts = {}
        for value in row["values"]:
            if value["scope"] != settings.scope:
                continue
            raw = value["raw_value"].strip()
            if raw in DASHES:
                self.amounts[value["year"]] = (Decimal(0), raw)
                continue
            try:
                self.amounts[value["year"]] = (parse_number(raw, _separator(raw)), raw)
            except ValueError:
                pass
        self.metric = None
        self.rule = "unassigned"

    def amount(self, year):
        return self.amounts.get(year, (None, ""))[0]


def _runs(pages, kind):
    """Consecutive-page groups of one statement type within one document."""
    order = {"left": 0, "full": 0, "right": 1}
    by_document = {}
    for page in pages:
        for panel in sorted(page.get("panels", []), key=lambda p: order.get(p["side"], 0)):
            if panel.get("statement") == kind and panel.get("rows"):
                by_document.setdefault(page["sha256"], []).append((page["page"], panel))
    for panels in by_document.values():
        run, last = [], None
        for number, panel in panels:
            if last is not None and number - last > 1:
                yield run
                run = []
            run.append(panel)
            last = number
        if run:
            yield run


def _tolerance(rows):
    # Statements in thousands or millions can differ by rounding; euros cannot.
    return Decimal(0) if all(r.row["scale"] == 1 for r in rows) else Decimal(2)


class _Builder:
    def __init__(self, settings):
        self.settings = settings
        self.facts = []
        self.report = []
        # Lines classified by rule inside a reconciled section or segment.
        self.settled = set()

    def settle(self, rows):
        self.settled.update((r.row["document_id"], r.row["page"], r.row["row_id"])
                            for r in rows if r.rule != "residual")

    def money(self, printed, row):
        return printed * Decimal(row.row["scale"]) / self.settings.money_scale

    def emit(self, metric, year, value, rows, formula, check, source="reconciled statement section",
             per_share=False, quote=None):
        first = rows[0].row
        components = [{"file": r.row["file"], "document_id": r.row["document_id"],
                       "page": r.row["page"], "panel": r.row["panel"], "row_id": r.row["row_id"],
                       "label": r.row["label"], "quote": r.row["quote"],
                       "raw_value": r.amounts.get(year, (None, ""))[1]} for r in rows]
        self.facts.append({
            "id": uuid.uuid4().hex, "metric_id": metric, "year": year,
            "scope": self.settings.scope, "currency": self.settings.currency,
            "value": str(value), "raw_value": "", "scale": 1,
            "file": first["file"], "source_file": first["file"], "page": first["page"],
            "panel": first["panel"], "document_id": first["document_id"],
            "row_label": formula, "section": first.get("section", ""),
            "quote": (quote or "; ".join(c["quote"] for c in components))[:2000],
            "context_quote": f"{first['statement']} statement · {self.settings.scope} · {year} · "
                             f"{self.settings.currency}",
            "mapping_source": source, "formula": formula, "components": components,
            "reconciliation": check, "unit": "per_share" if per_share else "money",
            "status": "candidate",
        })

    def sum_rows(self, metric, year, rows, check, sign=1):
        printed = sum(r.amount(year) for r in rows)
        value = self.money(printed, rows[0]) * sign
        names = " + ".join(r.row["label"] for r in rows)
        formula = names if sign == 1 else f"−({names})"
        self.emit(metric, year, value, rows, formula, check)

    def zero(self, metric, year, total_row, section_name, check):
        self.emit(metric, year, Decimal(0), [total_row],
                  f"Not presented as a separate line in {section_name}",
                  check, source="not presented; reconciled section",
                  quote=f"No separate line; {section_name} lines reconcile to: {total_row.row['quote']}")


def _note_ties(note, row, rows, combined, year, tolerance, settings):
    """A note total must equal the combined line (current, or current + non-current)."""
    total = note.get("note_total")
    if not total:
        return True
    printed = Decimal(total["value"]) * settings.money_scale / Decimal(row.row["scale"])
    current = row.amount(year)
    other = [r for r in rows if r is not row and r.metric == combined or (
        r is not row and getattr(r, "section", None) in ("NCA", "NCL")
        and _has(r.label, "trade") and _has(r.label, "other")
        and _has(r.label, "receivable" if combined == "combined_receivables" else "payable"))]
    candidates = [current] + [current + r.amount(year) for r in other if r.amount(year) is not None]
    return any(abs(printed - c) <= tolerance for c in candidates)


def _ambiguous(metric, residual_rows):
    """A residual line that could be this item blocks a reconciled zero."""
    return any(model_mapping_rejection(r.row["label"], metric) is None for r in residual_rows)


def _check(label, reported, calculated, source):
    return {"label": label, "reported": str(reported), "calculated": str(calculated),
            "source": source}


def _model_metric(model, row):
    return model.get((row.row["document_id"], row.row["page"], row.row["row_id"]))


# ------------------------------------------------------------------ builders

def _balance(run, settings, model, notes, build):
    rows = [_Row(r, settings) for panel in run for r in panel["rows"]]
    for r in rows:
        total = _balance_total(r.label)
        if total:
            r.metric, r.rule = total, "total"
            continue
        section = _balance_section(r.row.get("section", ""))
        r.section = section
        r.metric, r.rule = _balance_item(r.label, section, settings.scope)
        suggested = _model_metric(model, r)
        if r.rule == "residual" and suggested in BALANCE_SECTIONS.get(section, ([],))[0]:
            r.metric, r.rule = suggested, "model"
    totals = {r.metric: r for r in rows if r.rule == "total"}
    subtotal_50 = next((r for r in rows if r.metric == "balance_sheet_50"), None)
    tolerance = _tolerance(rows)
    for year in range(settings.latest_year - 5, settings.latest_year + 1):
        def printed(metric):
            row = totals.get(metric)
            return row.amount(year) if row else None
        section_totals = {
            "CA": printed("balance_sheet_17"), "CL": printed("balance_sheet_37"),
            "EQ": printed("balance_sheet_52"),
            "NCA": printed("balance_sheet_28"), "NCL": printed("balance_sheet_44"),
        }
        derived = {}
        assets, liabilities = printed("balance_sheet_29"), printed("balance_sheet_45")
        if liabilities is None and printed("EL") is not None and section_totals["EQ"] is not None:
            liabilities = printed("EL") - section_totals["EQ"]
            derived["balance_sheet_45"] = ("Total equity and liabilities − total equity",
                                           [totals["EL"], totals["balance_sheet_52"]], liabilities)
        if section_totals["NCA"] is None and assets is not None and section_totals["CA"] is not None:
            section_totals["NCA"] = assets - section_totals["CA"]
            derived["balance_sheet_28"] = ("Total assets − total current assets",
                                           [totals["balance_sheet_29"], totals["balance_sheet_17"]],
                                           section_totals["NCA"])
        if section_totals["NCL"] is None and liabilities is not None and section_totals["CL"] is not None:
            section_totals["NCL"] = liabilities - section_totals["CL"]
            sources = [totals.get("balance_sheet_45") or totals["EL"], totals["balance_sheet_37"]]
            derived["balance_sheet_44"] = ("Total liabilities − total current liabilities",
                                           sources, section_totals["NCL"])
        reconciled = set()
        for name, (metrics, total_metric, residual) in BALANCE_SECTIONS.items():
            total = section_totals[name]
            items = [r for r in rows if getattr(r, "section", None) == name and r.rule not in
                     ("total", "subtotal")]
            if total is None or not items or any(r.amount(year) is None for r in items):
                continue
            calculated = sum(r.amount(year) for r in items)
            source_row = totals.get(total_metric) or (
                derived[total_metric][1][0] if total_metric in derived else None)
            if abs(calculated - total) > tolerance:
                build.report.append({"year": year, "statement": "balance", "section": name,
                                     "status": "mismatch", "reported": str(total),
                                     "calculated": str(calculated),
                                     "page": items[0].row["page"]})
                continue
            reconciled.add(name)
            build.settle(items + [r for r in rows if r.rule == "total"]
                         + [r for r in rows if getattr(r, "section", None) == name and r.rule == "subtotal"])
            label = f"{name} lines = printed section total"
            check = _check(label, total, calculated,
                           (source_row.row["quote"] if source_row else ""))
            groups = {}
            for r in items:
                groups.setdefault(r.metric, []).append(r)
            # A combined trade-and-other line is split only with a cited note
            # total for the trade component; otherwise it stays in the residual.
            for combined, trade, rest in (("combined_receivables", "balance_sheet_10", "balance_sheet_16"),
                                          ("combined_payables", "balance_sheet_31", "balance_sheet_36")):
                for r in groups.pop(combined, []):
                    note = notes.get((trade, year, r.row["document_id"]))
                    r.split = ((trade, note) if note is not None and trade not in groups and _note_ties(
                        note, r, rows, combined, year, tolerance, settings) else None)
                    groups.setdefault(rest, []).append(r)
            for metric, members in groups.items():
                split = [r for r in members if getattr(r, "split", None)]
                if not split:
                    build.sum_rows(metric, year, members, check)
                    continue
                trade_metric, note = split[0].split
                other_printed = sum(r.amount(year) for r in members)
                trade_printed = note["value"] * settings.money_scale / Decimal(split[0].row["scale"])
                build.emit(trade_metric, year, note["value"], [split[0]],
                           f"{note['label']} (note p.{note['page']}), assumed current",
                           check | {"note": note["quote"]}, quote=note["quote"])
                build.emit(metric, year, build.money(other_printed - trade_printed, members[0]),
                           members, f"{' + '.join(r.row['label'] for r in members)} − "
                                    f"{note['label']} (note p.{note['page']})", check)
            if name == "EQ" and subtotal_50 is not None and subtotal_50.rule == "subtotal" \
                    and subtotal_50.amount(year) is not None:
                build.emit("balance_sheet_50", year, build.money(subtotal_50.amount(year), subtotal_50),
                           [subtotal_50], subtotal_50.row["label"], check,
                           source="printed statement subtotal")
            if name == "EQ" and subtotal_50 is None:
                parent = [r for r in items if r.metric != "balance_sheet_51"]
                if parent:
                    value = sum(r.amount(year) for r in parent)
                    build.emit("balance_sheet_50", year, build.money(value, parent[0]), parent,
                               "Equity lines excluding non-controlling interests", check)
            residual_rows = [r for r in items if r.rule == "residual"]
            present = set(groups) | {s.split[0] for s in items if getattr(s, "split", None)}
            for metric in metrics:
                if metric in present or metric == residual and residual_rows:
                    continue
                if (metric in ("balance_sheet_10", "balance_sheet_31") and any(
                        r.metric in ("combined_receivables", "combined_payables") for r in items)) \
                        or _ambiguous(metric, residual_rows):
                    continue
                anchor = source_row or items[-1]
                build.zero(metric, year, anchor, f"the {name} section", check)
        for metric, row in totals.items():
            if metric.startswith("balance_sheet_") and row.amount(year) is not None:
                build.emit(metric, year, build.money(row.amount(year), row), [row],
                           row.row["label"], None, source="printed statement total")
        for metric, (formula, sources, value) in derived.items():
            if metric == "balance_sheet_28" and "NCA" not in reconciled:
                continue
            if metric == "balance_sheet_44" and "NCL" not in reconciled:
                continue
            build.emit(metric, year, build.money(value, sources[0]), sources, formula, None,
                       source="derived from printed totals")


def _income(run, settings, model, build):
    rows = [_Row(r, settings) for panel in run for r in panel["rows"]]
    revenue = next((i for i, r in enumerate(rows) if _income_item(r.label, "S0")[0] ==
                    "income_statement_8"), None)
    if revenue is None:
        return
    anchors = {}
    for i, r in enumerate(rows[revenue:], revenue):
        anchor = _income_anchor(r.label)
        if anchor and anchor not in anchors and all(i > j for j in anchors.values()):
            anchors[anchor] = i
    net = next((i for i, r in enumerate(rows) if i > anchors.get("income_statement_25", len(rows))
                and _is_net_profit(r.label)), None)
    if "income_statement_16" not in anchors or "income_statement_25" not in anchors or net is None:
        return
    gross, ebit, pbt = (anchors.get("income_statement_10"), anchors["income_statement_16"],
                        anchors["income_statement_25"])
    segments = [("S0", revenue, gross if gross is not None else ebit),
                ("S1", gross, ebit) if gross is not None else None,
                ("S2", ebit, pbt), ("S3", pbt, net)]
    tolerance = _tolerance(rows)
    for year in range(settings.latest_year - 5, settings.latest_year + 1):
        if any(r.amount(year) is None for r in rows[revenue:net + 1]):
            continue
        # Most IFRS statements print expenses in parentheses; a few print them
        # unsigned. Accept whichever convention makes every printed subtotal tie.
        for expenses_negative in (True, False):
            def signed(r, metric):
                value = r.amount(year)
                if not expenses_negative and metric in EXPENSE_ROWS | {"income_statement_9"}:
                    return -value
                return value
            results, ok = [], True
            for segment in filter(None, segments):
                name, start, end = segment
                span = rows[start + (0 if name == "S0" else 1):end]
                if name == "S0" and gross is None:
                    span = [r for r in span if _income_anchor(r.label) is None]
                items = []
                for r in span:
                    metric, rule = _income_item(r.label, "S1" if name == "S0" and gross is None and
                                                _income_item(r.label, "S0")[0] is None else name)
                    if rule == "subtotal":
                        continue
                    suggested = _model_metric(model, r)
                    if rule == "residual" and suggested and suggested.startswith("income_statement_"):
                        metric, rule = suggested, "model"
                    if metric is None:
                        ok = False
                        break
                    r.rule = rule
                    items.append((r, metric, rule))
                if not ok:
                    break
                start_value = Decimal(0) if name == "S0" else rows[start].amount(year)
                calculated = start_value + sum(signed(r, m) for r, m, _ in items)
                if abs(calculated - rows[end].amount(year)) > tolerance:
                    ok = False
                    break
                results.append((name, items, rows[end], calculated))
            if ok:
                break
        else:
            build.report.append({"year": year, "statement": "income", "status": "mismatch",
                                 "page": rows[revenue].row["page"]})
            continue
        # Lines after profit for the year (other comprehensive income,
        # attribution, per-share amounts) have no model-dependent template row.
        build.settle([r for _, items, end, _ in results for r, _, _ in items]
                     + [r for _, _, r, _ in results] + rows[net:])
        segment_metrics = {"S0": ["income_statement_8", "income_statement_9"],
                           "S1": ["income_statement_11", "income_statement_12", "income_statement_13",
                                  "income_statement_14", "income_statement_15"],
                           "S2": ["income_statement_20", "income_statement_21", "income_statement_23",
                                  "income_statement_24"],
                           "S3": ["income_statement_26", "income_statement_27"]}
        for name, items, end_row, calculated in results:
            check = _check(f"Lines to {end_row.row['label']} = printed subtotal",
                           end_row.amount(year), calculated, end_row.row["quote"])
            groups = {}
            for r, metric, _ in items:
                groups.setdefault(metric, []).append(r)
            for metric, members in groups.items():
                printed = sum(signed(r, metric) for r in members)
                value = -printed if metric in EXPENSE_ROWS else printed
                build.emit(metric, year, build.money(value, members[0]), members,
                           (" + ".join(r.row["label"] for r in members)
                            + (" (expense, shown positive)" if metric in EXPENSE_ROWS else "")),
                           check)
            residual_rows = [r for r, _, rule in items if rule == "residual"]
            for metric in segment_metrics[name]:
                if metric in groups or metric == "income_statement_8" or (
                        name == "S0" and gross is None and metric == "income_statement_9"):
                    continue
                if _ambiguous(metric, residual_rows):
                    continue
                build.zero(metric, year, end_row, f"the lines before {end_row.row['label']}", check)
        for anchor_metric, index in list(anchors.items()) + [("income_statement_28", net)]:
            r = rows[index]
            build.emit(anchor_metric, year, build.money(r.amount(year), r), [r], r.row["label"],
                       None, source="printed statement subtotal")
        # Attribution: the first owners / non-controlling pair after net profit
        # that adds up to it (a later pair attributes comprehensive income).
        net_row = rows[net]
        following = rows[net + 1:]
        for i, owners in enumerate(following):
            if not _has(owners.label, "owner", "shareholder", "parent", "equity holder",
                        "attributable", "μετοχ", "ιδιοκτητ") or _has(
                    owners.label, "non controlling", "minority", "μη ελεγχ"):
                continue
            nci = following[i + 1] if i + 1 < len(following) else None
            nci = nci if nci and _has(nci.label, "non controlling", "minority", "μη ελεγχ") else None
            if owners.amount(year) is None or (nci and nci.amount(year) is None):
                continue
            calculated = owners.amount(year) + (nci.amount(year) if nci else 0)
            if abs(calculated - net_row.amount(year)) > tolerance:
                continue
            check = _check("Owners + non-controlling interests = profit for the year",
                           net_row.amount(year), calculated, net_row.row["quote"])
            build.emit("income_statement_29", year, build.money(owners.amount(year), owners),
                       [owners], owners.row["label"], check)
            if nci:
                build.emit("income_statement_30", year, build.money(nci.amount(year), nci), [nci],
                           nci.row["label"], check)
            else:
                build.zero("income_statement_30", year, owners, "the profit attribution", check)
            break
    # Earnings per share are per-share amounts: the statement's money scale
    # does not apply.
    seen = set()
    for r in rows[net + 1:]:
        text = f"{r.label} {_plain_label(r.row.get('section', ''))}"
        metric = ("market_inputs_11" if _has(text, "diluted", "απομειωμεν") else
                  "market_inputs_10" if _has(text, "basic", "βασικ") else None)
        if not metric or metric in seen:
            continue
        seen.add(metric)
        for year in range(settings.latest_year - 5, settings.latest_year + 1):
            value = r.amount(year)
            section = r.row.get("section", "")
            name = (f"{section} {r.row['label']}" if _has(_plain_label(section), "per share",
                                                          "ανα μετοχ") else r.row["label"])
            if value is not None and r.amounts[year][1] not in DASHES:
                build.emit(metric, year, value, [r], name.strip(), None,
                           source="printed per-share amount", per_share=True)


def _cash(run, settings, model, build):
    rows = [_Row(r, settings) for panel in run for r in panel["rows"]]
    nets = {}
    for i, r in enumerate(rows):
        metric = _cash_net(r.label)
        if metric and metric not in nets:
            nets[metric] = i
    if "cash_flow_statement_37" not in nets or "cash_flow_statement_18" not in nets:
        return
    after_wc, first = False, True
    for i, r in enumerate(rows):
        if i in nets.values():
            r.metric, r.rule = [m for m, j in nets.items() if j == i][0], "total"
            continue
        if i > nets["cash_flow_statement_37"]:
            r.section, r.metric = "BRIDGE", _cash_bridge(r.label)
            r.rule = "subtotal" if r.metric == "subtotal" else "rule" if r.metric != "other" else "bridge"
            continue
        r.section = _cash_section(r.row.get("section", ""))
        r.metric, r.rule = _cash_item(r.label, r.section, after_wc, first and r.section == "OP")
        if r.section == "OP":
            first = False
            after_wc = after_wc or r.rule == "working" or (
                r.rule == "subtotal" and _has(r.label, "before", "προ"))
        suggested = _model_metric(model, r)
        if r.rule == "residual" and suggested and suggested.startswith("cash_flow_statement_"):
            r.metric, r.rule = suggested, "model"
    sections = {"OP": ("cash_flow_statement_18", ["cash_flow_statement_%d" % n for n in range(8, 18)]),
                "INV": ("cash_flow_statement_27", ["cash_flow_statement_%d" % n for n in range(20, 27)]),
                "FIN": ("cash_flow_statement_37", ["cash_flow_statement_%d" % n for n in range(29, 37)])}
    tolerance = _tolerance(rows)
    for year in range(settings.latest_year - 5, settings.latest_year + 1):
        reconciled = {}
        for name, (net_metric, metrics) in sections.items():
            net_row = rows[nets[net_metric]]
            items = [r for r in rows if getattr(r, "section", None) == name
                     and r.rule not in ("total", "subtotal")]
            if not items or net_row.amount(year) is None or any(r.amount(year) is None or r.metric is None
                                                               for r in items):
                continue
            calculated = sum(r.amount(year) for r in items)
            if abs(calculated - net_row.amount(year)) > tolerance:
                build.report.append({"year": year, "statement": "cash", "section": name,
                                     "status": "mismatch", "reported": str(net_row.amount(year)),
                                     "calculated": str(calculated), "page": net_row.row["page"]})
                continue
            reconciled[name] = net_row
            build.settle(items + [net_row] + [r for r in rows if getattr(r, "section", None) == name
                                              and r.rule == "subtotal"])
            check = _check(f"{name} lines = {net_row.row['label']}", net_row.amount(year),
                           calculated, net_row.row["quote"])
            groups = {}
            for r in items:
                groups.setdefault(r.metric, []).append(r)
            for metric, members in groups.items():
                build.sum_rows(metric, year, members, check)
                if metric == "cash_flow_statement_9":
                    build.sum_rows("income_statement_17", year, members, check)
            build.emit(net_metric, year, build.money(net_row.amount(year), net_row), [net_row],
                       net_row.row["label"], check, source="printed statement total")
            residual_rows = [r for r in items if r.rule == "residual"]
            for metric in metrics:
                # Working-capital lines are recognised by their change wording,
                # and paid lines by "paid"; an adjustment before working capital
                # cannot be an operating cash flow after it.
                candidates = ([] if metric in ("cash_flow_statement_%d" % n for n in (11, 12, 13, 14, 17))
                              else [r for r in residual_rows if _has(r.label, "paid", "καταβλ", "πληρω")]
                              if metric in ("cash_flow_statement_15", "cash_flow_statement_16")
                              else residual_rows)
                if metric in groups or metric == "cash_flow_statement_8" or _ambiguous(metric, candidates):
                    continue
                build.zero(metric, year, net_row, f"the {name.lower()} section", check)
        bridge = [r for r in rows if getattr(r, "section", None) == "BRIDGE"]
        opening = next((r for r in bridge if r.metric == "cash_flow_statement_40"), None)
        closing = next((r for r in bridge if r.metric == "cash_flow_statement_41"), None)
        for row in (opening, closing):
            if row is not None and row.amount(year) is not None:
                build.emit(row.metric, year, build.money(row.amount(year), row), [row],
                           row.row["label"], None, source="printed statement total")
        movements = [r for r in bridge if r.rule in ("rule", "bridge")
                     and r.metric not in ("cash_flow_statement_40", "cash_flow_statement_41")]
        if len(reconciled) == 3 and opening and closing and all(
                r.amount(year) is not None for r in movements + [opening, closing]):
            calculated = (opening.amount(year) + sum(n.amount(year) for n in reconciled.values())
                          + sum(r.amount(year) for r in movements))
            if abs(calculated - closing.amount(year)) <= tolerance:
                build.settle(bridge)
                check = _check("Opening cash + net flows + other movements = closing cash",
                               closing.amount(year), calculated, closing.row["quote"])
                fx = [r for r in movements if r.metric == "cash_flow_statement_39"]
                if fx:
                    build.sum_rows("cash_flow_statement_39", year, fx, check)
                elif not [r for r in movements if r.rule == "bridge" and r.amount(year) != 0]:
                    build.zero("cash_flow_statement_39", year, closing, "the cash reconciliation", check)


_MONTHS = {"january": 1, "ιανουαριου": 1, "december": 12, "δεκεμβριου": 12}


def _balance_date(quote):
    text = _plain_label(quote)
    match = re.search(r"\b(\d{1,2})\s+(january|december|ιανουαριου|δεκεμβριου)\s+((?:19|20)\d{2})\b", text)
    if match:
        return int(match[1]), _MONTHS[match[2]], int(match[3])
    match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})\b", quote)
    if match:
        return int(match[1]), int(match[2]), int(match[3])
    return None


def _equity_item(label, after_total):
    if _has(label, "total comprehensive", "συνολικα συνολικ", "συγκεντρωτικ"):
        return "subtotal"
    if not after_total:
        if _has(label, "profit", "loss", "result", "κερδ", "ζημ", "αποτελεσμ") and not _has(
                label, "comprehensive", "fair value", "actuarial", "συνολικ"):
            return "changes_in_equity_8"
        return "changes_in_equity_9"
    if _has(label, "dividend", "μερισμ"):
        return "changes_in_equity_12"
    if _has(label, "treasury", "own shares", "ιδιες μετοχ"):
        return "changes_in_equity_11"
    if _has(label, "subsidiar", "minority", "non controlling", "θυγατρ", "μη ελεγχ", "μειοψηφ"):
        return "changes_in_equity_13"
    if _has(label, "share capital", "capital increase", "share issue", "issue of shares", "option",
            "αυξηση μετοχικου", "εκδοση"):
        return "changes_in_equity_10"
    return "changes_in_equity_14"


def _equity(run, settings, build):
    """Total-equity column of the statement of changes in equity, per year."""
    rows = []
    for panel in run:
        for r in panel.get("equity_rows", []):
            if r["scope"] != settings.scope:
                continue
            row = _Row({**r, "values": [], "section": ""}, settings)
            raw = r["raw_total"].strip()
            try:
                row.total = (Decimal(0) if raw in DASHES else parse_number(raw, _separator(raw)), raw)
            except ValueError:
                row.total = (None, raw)
            rows.append(row)
    periods, current = [], None
    for r in rows:
        date = _balance_date(r.row["quote"]) if _has(r.label, "balance", "υπολοιπ") else None
        if date:
            day, month, year = date
            restated = _has(r.label, "restat", "adjust", "αναμορφ", "αναπροσαρμ")
            if current and not current.get("closing") and restated:
                current.update(opening=r, items=[])
            elif current is None or current.get("closing"):
                current = {"year": year + 1 if (day, month) == (31, 12) else year,
                           "opening": r, "items": []}
                periods.append(current)
            else:
                current["closing"] = r
        elif current and not current.get("closing"):
            current["items"].append(r)
    tolerance = _tolerance(rows) if rows else Decimal(0)
    for period in periods:
        year, opening, closing = period["year"], period["opening"], period.get("closing")
        if closing is None or not settings.latest_year - 5 <= year <= settings.latest_year:
            continue
        for r in period["items"]:
            r.amounts[year] = r.total
        opening.amounts[year], closing.amounts[year] = opening.total, closing.total
        if any(r.total[0] is None for r in period["items"] + [opening, closing]):
            continue
        after_total, groups, subtotal = False, {}, None
        if not any(_equity_item(r.label, False) == "subtotal" for r in period["items"]):
            after_total = None  # no printed total comprehensive income line
        for r in period["items"]:
            kind = _equity_item(r.label, bool(after_total))
            if kind == "subtotal":
                subtotal, after_total = r, True
                continue
            if after_total is None and kind == "changes_in_equity_9" and not _has(
                    r.label, "comprehensive", "fair value", "hedg", "translation", "actuarial",
                    "revaluation", "remeasure", "συνολικ"):
                kind = _equity_item(r.label, True)
            groups.setdefault(kind, []).append(r)
        movements = [r for members in groups.values() for r in members]
        calculated = opening.total[0] + sum(r.total[0] for r in movements)
        comprehensive = sum(r.total[0] for m in ("changes_in_equity_8", "changes_in_equity_9")
                            for r in groups.get(m, []))
        if (abs(calculated - closing.total[0]) > tolerance or "changes_in_equity_8" not in groups
                or (subtotal is not None and abs(comprehensive - subtotal.total[0]) > tolerance)):
            build.report.append({"year": year, "statement": "equity", "status": "mismatch",
                                 "reported": str(closing.total[0]), "calculated": str(calculated),
                                 "page": closing.row["page"]})
            continue
        check = _check("Opening equity + movements = closing equity", closing.total[0], calculated,
                       closing.row["quote"])
        build.emit("changes_in_equity_7", year, build.money(opening.total[0], opening), [opening],
                   opening.row["label"], check)
        build.emit("changes_in_equity_15", year, build.money(closing.total[0], closing), [closing],
                   closing.row["label"], check)
        for metric, members in groups.items():
            build.sum_rows(metric, year, members, check)
        for n in range(8, 15):
            metric = f"changes_in_equity_{n}"
            if metric not in groups and not (n == 14 and groups.get(metric)):
                build.zero(metric, year, closing, "the statement of changes in equity", check)


def statement_facts(pages, settings, candidates=(), notes=()):
    """Reconciled template inputs from complete primary statements.

    ``candidates`` supply earlier label mappings (exact or model) that may
    move a residual line to a specific row. ``notes`` supply note totals that
    split a combined trade-and-other line. Returns (facts, report).
    """
    model = {(c.get("document_id"), c.get("page"), c.get("row_id")): c["metric_id"]
             for c in candidates if c.get("mapping_source") == "Groq label mapping"}
    note_values = {}
    for c in notes:
        if c["metric_id"] in ("balance_sheet_10", "balance_sheet_31"):
            key = (c["metric_id"], c["year"], c.get("document_id"))
            note_values.setdefault(key, []).append(c)
    single = {}
    for key, facts in note_values.items():
        if len({Decimal(f["value"]) for f in facts}) == 1:
            f = facts[0]
            single[key] = {"value": Decimal(f["value"]), "page": f["page"],
                           "label": f.get("row_label", "note total"), "quote": f.get("quote", ""),
                           "note_total": f.get("note_total")}
    build = _Builder(settings)
    for run in _runs(pages, "balance"):
        _balance(run, settings, model, single, build)
    for run in _runs(pages, "income"):
        _income(run, settings, model, build)
    for run in _runs(pages, "cash"):
        _cash(run, settings, model, build)
    for run in _equity_runs(pages):
        _equity(run, settings, build)
    # Cash at the end of the cash-flow statement that equals balance-sheet
    # cash leaves no difference to explain.
    by_key = {}
    for f in build.facts:
        by_key.setdefault((f["metric_id"], f["year"], f["document_id"]), set()).add(Decimal(f["value"]))
    for f in [f for f in build.facts if f["metric_id"] == "cash_flow_statement_41"]:
        cash = by_key.get(("balance_sheet_8", f["year"], f["document_id"]), set())
        if len(cash) == 1 and Decimal(f["value"]) in cash:
            build.facts.append(f | {"id": uuid.uuid4().hex, "metric_id": "cash_flow_statement_42",
                                    "value": "0", "formula": "Closing cash in the cash-flow "
                                    "statement equals balance-sheet cash",
                                    "row_label": "Closing cash = balance-sheet cash",
                                    "mapping_source": "cross-statement check"})
    for f in [f for f in build.facts if f["metric_id"] == "balance_sheet_53"
              and f["mapping_source"] == "not presented; reconciled section"]:
        for metric, formula in (("income_statement_31", "No preferred equity presented, so no "
                                 "preferred dividends"),
                                ("market_inputs_15", "No preferred equity presented")):
            build.facts.append(f | {"id": uuid.uuid4().hex, "metric_id": metric, "value": "0",
                                    "formula": formula, "row_label": formula})
    return build.facts, build.report


def _equity_runs(pages):
    by_document = {}
    for page in pages:
        for panel in page.get("panels", []):
            if panel.get("equity_rows"):
                by_document.setdefault(page["sha256"], []).append(panel)
    yield from by_document.values()


def settled_rows(pages, settings):
    """Statement lines that need no model classification (see mapping_tasks)."""
    build = _Builder(settings)
    for run in _runs(pages, "balance"):
        _balance(run, settings, {}, {}, build)
    for run in _runs(pages, "income"):
        _income(run, settings, {}, build)
    for run in _runs(pages, "cash"):
        _cash(run, settings, {}, build)
    return frozenset(build.settled)


def apply_statement_facts(candidates, facts):
    """Reconciled statement facts replace per-line facts for the same pages.

    A per-line candidate from a statement page is superseded when that page's
    reconciled statement already supplies the metric and year; candidates from
    notes and other pages are kept so real disagreements stay visible.
    """
    covered = {(f["metric_id"], f["year"], f["document_id"], c["page"])
               for f in facts for c in f.get("components", [])}
    kept = [c for c in candidates
            if (c["metric_id"], c["year"], c.get("document_id"), c.get("page")) not in covered]
    return kept + facts
