"""Retrieve cited note evidence and calculate only auditable financial inputs.

The language model may classify unfamiliar statement labels, but it cannot
invent note values or perform arithmetic. Note rows are selected by their
printed heading, dated columns, reporting scope and exact label. Calculated
figures retain all operands and, where present, reconcile to a printed total.
"""

import re
import uuid
from collections import defaultdict
from decimal import Decimal

from .engine import _candidates_from_row, _label, _meaning_allowed, _separator, parse_number


def _amount(row, scope, year, settings, shares=False):
    column = next((v for v in row["values"] if v["year"] == year and v["scope"] == scope), None)
    if not column:
        return None
    raw = column["raw_value"]
    value = parse_number(raw, _separator(raw))
    return value / settings.share_scale if shares else value * Decimal(row["scale"]) / settings.money_scale


def _derived(metric_id, rows, settings, year, value, formula, check=None):
    first = rows[0]
    units = "shares" if metric_id == "market_inputs_8" else "money"
    sources = [{"file": r["file"], "document_id": r["document_id"],
                "page": r["page"], "panel": r["panel"], "row_id": r.get("row_id"),
                "label": r["label"], "quote": r["quote"],
                "raw_value": next(v["raw_value"] for v in r["values"]
                                  if v["scope"] == settings.scope and v["year"] == year)}
               for r in rows]
    return {"id": uuid.uuid4().hex, "metric_id": metric_id, "year": year,
            "scope": settings.scope, "currency": settings.currency, "value": str(value),
            "raw_value": "", "scale": 1, "file": first["file"],
            "source_file": first["file"], "page": first["page"],
            "panel": first["panel"], "document_id": first["document_id"],
            "row_label": formula, "quote": "; ".join(s["quote"] for s in sources),
            "context_quote": f"{first.get('note_title') or first['statement']} · {settings.scope} · {year} · {settings.currency}",
            "mapping_source": "reconciled source rows" if check else "sum of source rows",
            "formula": formula, "components": sources, "reconciliation": check,
            "unit": units, "status": "candidate"}


def _panels_for(pages, search, query):
    if search is None:
        return [panel | {"file": page["file"], "document_id": page["sha256"], "page": page["page"]}
                for page in pages for panel in page["panels"]]
    hits = search(query, limit=24, all_terms=True)
    wanted = {(h["document_id"], h["page"], h["side"]) for h in hits}
    return [panel | {"file": page["file"], "document_id": page["sha256"], "page": page["page"]}
            for page in pages for panel in page["panels"]
            if (page["sha256"], page["page"], panel["side"]) in wanted]


def _source_rows(panels, title):
    for panel in panels:
        rows = [r for r in panel.get("note_rows", []) if title in _label(r.get("note_title", ""))]
        if rows:
            yield panel, rows


def _cf_depreciation(pages, settings):
    for page in pages:
        for panel in page["panels"]:
            if panel.get("statement") != "cash":
                continue
            rows = panel.get("rows", [])
            base = next((r for r in rows if _label(r["label"]).startswith("depreciation and amortization")
                         and "non current assets" in _label(r["label"])), None)
            leases = next((r for r in rows if _label(r["label"]) == "depreciation of right of use assets"), None)
            if not base or not leases:
                continue
            for year in range(settings.latest_year - 5, settings.latest_year + 1):
                a, b = (_amount(r, settings.scope, year, settings) for r in (base, leases))
                if a is None or b is None:
                    continue
                for metric in ("cash_flow_statement_9", "income_statement_17"):
                    yield _derived(metric, [base, leases], settings, year, a + b,
                                   "D&A on non-current assets + right-of-use depreciation")


def _finance_cost(panels, settings):
    for _panel, rows in _source_rows(panels, "finance cost"):
        names = [_label(r["label"]) for r in rows]
        names = [name.removesuffix("*").strip() for name in names]
        parts = ("interest on borrowings", "interest on leases", "other interest expenses")
        all_parts = parts + ("realised losses from derivatives accounted at fvtpl",
                             "losses from valuation of derivatives accounted at fvtpl",
                             "bank commissions", "commitment fees")
        if not all(names.count(name) == 1 for name in all_parts) or names.count("total finance cost") != 1:
            continue
        components = [rows[names.index(name)] for name in parts]
        all_rows = [rows[names.index(name)] for name in all_parts]
        total = rows[names.index("total finance cost")]
        for year in range(settings.latest_year - 5, settings.latest_year + 1):
            vals = [_amount(r, settings.scope, year, settings) for r in all_rows]
            reported = _amount(total, settings.scope, year, settings)
            if reported is None or any(v is None for v in vals) or sum(vals) != reported:
                continue
            amount = sum(_amount(r, settings.scope, year, settings) for r in components)
            yield _derived("income_statement_22", components, settings, year, amount,
                           "Borrowing interest + lease interest + other interest",
                           check={"label": "All seven finance-cost components = reported finance cost",
                                  "reported": str(reported), "calculated": str(sum(vals)),
                                  "source": total["quote"]})


def _trade_receivables(panels, settings):
    for _panel, rows in _source_rows(panels, "trade and other receivables"):
        for i, row in enumerate(rows):
            if _label(row["label"]) != "trade receivables" or len(rows) < i + 4:
                continue
            components = rows[i:i + 3]
            subtotal = rows[i + 3]
            if ([_label(x["label"]) for x in components] !=
                ["trade receivables", "allowance for doubtful debts", "related parties"]
                or subtotal["label"].strip()):
                continue
            for year in range(settings.latest_year - 5, settings.latest_year + 1):
                vals = [_amount(r, settings.scope, year, settings) for r in components]
                reported = _amount(subtotal, settings.scope, year, settings)
                if reported is None or any(v is None for v in vals) or sum(vals) != reported:
                    continue
                yield _derived("balance_sheet_10", components + [subtotal], settings, year,
                               reported, "Gross trade receivables − loss allowance + trade related parties",
                               check={"label": "Component sum = printed trade subtotal",
                                      "reported": str(reported), "calculated": str(sum(vals)),
                                      "source": subtotal["quote"]})


def _weighted_shares(panels, pages, settings):
    by_page = {(p["sha256"], p["page"]): p for p in pages}
    rejected = []
    for _panel, rows in _source_rows(panels, "earnings per share"):
        for row in rows:
            if _label(row["label"]) != "weighted average number of ordinary shares for the purposes of basic earnings per share":
                continue
            if row["currency"] != settings.currency:
                continue
            for candidate in _candidates_from_row(row, "market_inputs_9", "dated earnings-per-share note",
                                                   by_page | {(p["file"], p["page"]): p for p in pages},
                                                   settings, rejected):
                yield candidate


def _direct_note_facts(pages, settings):
    """Accept precise note labels only with a dated, scoped source column.

    Component lines such as 'interest on borrowings', gross receivables, or
    generic financial assets do not establish a total by themselves.
    """
    lookup = {(key, page["page"]): page for page in pages
              for key in (page["sha256"], page["file"])}
    for page in pages:
        for panel in page.get("panels", []):
            # Some issuers disclose accrued interest expense as a separate
            # line in the cash-flow reconciliation rather than in the income
            # statement. An exact printed label is usable with its own citation.
            rows = panel.get("note_rows", []) + (panel.get("rows", []) if panel.get("statement") == "cash" else [])
            for row in rows:
                label = _label(row["label"])
                title = _label(row.get("note_title", ""))
                metric = None
                if label in ("interest expense", "total interest expense", "interest expenses") and (
                        panel.get("statement") == "cash" or
                        "finance" in title or "interest" in title or "borrow" in title):
                    metric = "income_statement_22"
                elif label in ("liquid short term investments", "short term marketable securities",
                               "short term investments") and (
                        "investment" in title or "financial asset" in title or "cash" in title):
                    metric = "balance_sheet_9"
                elif label in ("basic earnings per share", "basic earnings per share eur") and (
                        "earnings per share" in title):
                    metric = "market_inputs_10"
                if metric:
                    rejected = []
                    yield from _candidates_from_row(row, metric, "exact dated note concept",
                                                    lookup, settings, rejected)


def _narrative(panels, pages, settings):
    lookup = {(p["sha256"], p["page"]): p for p in pages}
    patterns = (
        ("market_inputs_7", re.compile(
            r"closing price of the share .{0,100}? on 31 December (20\d{2}) was Euro (\d+(?:\.\d+)?)", re.I | re.S),
         "share price data", 1),
        ("income_statement_18", re.compile(
            r"EBITDA of the (Group|Company) in (20\d{2}) was Euro ([\d,]+) thousand compared with Euro ([\d,]+) thousand in (20\d{2})", re.I),
         "earnings before interest", 1000),
    )
    from .engine import _candidates_from_row
    for metric_id, pattern, section, scale in patterns:
        for panel in panels:
            text = " ".join(panel["text"].split())
            if section not in text.casefold():
                continue
            for match in pattern.finditer(text):
                if metric_id == "market_inputs_7":
                    values = [(int(match[1]), match[2], settings.scope)]
                else:
                    scope = "consolidated" if match[1].casefold() == "group" else "standalone"
                    values = [(int(match[2]), match[3], scope), (int(match[5]), match[4], scope)]
                for year, raw, scope in values:
                    if scope != settings.scope or not settings.latest_year - 5 <= year <= settings.latest_year:
                        continue
                    row = {"row_id": f"{panel['page']}:{panel['side']}:{metric_id}:{year}",
                           "document_id": panel["document_id"], "file": panel["file"],
                           "page": panel["page"], "panel": panel["side"],
                           "statement": "note", "section": section, "label": section,
                           "quote": match[0], "currency": "EUR", "scale": scale,
                           "values": [{"year": year, "scope": scope, "header": str(year),
                                       "raw_value": raw, "x": 0}]}
                    rejected = []
                    for candidate in _candidates_from_row(row, metric_id, "dated directors-report disclosure",
                                                           lookup, settings, rejected):
                        yield candidate


def _reported_net_debt(panels, settings):
    pattern = re.compile(r"Net debt\s*\(Group\)\s*([\d,]+)\s*th\.\s*€", re.I)
    for panel in panels:
        text = " ".join(panel["text"].split())
        if not re.search(r"\bFY\s*25\b", text[:450], re.I):
            continue
        for match in pattern.finditer(text):
            year = 2025
            if year != settings.latest_year or settings.scope != "consolidated" or settings.currency != "EUR":
                continue
            amount = parse_number(match[1], ".") * Decimal(1000) / settings.money_scale
            yield {"id": uuid.uuid4().hex, "metric_id": "reported_net_debt",
                   "year": year, "scope": settings.scope, "currency": settings.currency,
                   "value": str(amount), "raw_value": match[1], "scale": 1000,
                   "file": panel["file"], "page": panel["page"], "panel": panel["side"],
                   "document_id": panel["document_id"], "quote": match[0],
                   "context_quote": "FY25 · Group · EUR thousands",
                   "mapping_source": "dated directors-report disclosure", "status": "candidate"}


def enrich_financial_evidence(pages, settings, search=None):
    """Query the persistent page index and validate only matched note panels."""
    cf = list(_cf_depreciation(pages, settings))
    finance = list(_finance_cost(_panels_for(pages, search, "interest borrowings finance"), settings))
    trade = list(_trade_receivables(_panels_for(pages, search, "trade receivables allowance"), settings))
    shares = list(_weighted_shares(_panels_for(pages, search, "weighted average ordinary shares"), pages, settings))
    direct = list(_direct_note_facts(pages, settings))
    narrative = list(_narrative(_panels_for(pages, search, "closing price share")
                                + _panels_for(pages, search, "ebitda group"), pages, settings))
    net_debt = list(_reported_net_debt(_panels_for(pages, search, "net debt group"), settings))
    unique = {}
    for fact in cf + finance + trade + shares + direct + narrative:
        unique.setdefault((fact["metric_id"], fact["year"], fact["value"],
                           fact["document_id"], fact["page"], fact["panel"]), fact)
    return list(unique.values()), net_debt


def merge_evidence(old, additions, selected_ids=()):
    """Recheck earlier model labels when upgrading a saved job without Groq."""
    keep = set(selected_ids)
    unique = {}
    removed = []
    for fact in old + additions:
        if (fact.get("mapping_source") == "Groq label mapping"
                and not _meaning_allowed({"label": fact.get("row_label", ""),
                                          "section": fact.get("section", "")}, fact["metric_id"])):
            if fact["id"] not in keep:
                removed.append(fact)
                continue
        key = (fact["metric_id"], fact["year"], Decimal(fact["value"]),
               fact.get("document_id"), fact.get("page"), fact.get("panel"))
        unique.setdefault(key, fact)
    merged = list(unique.values())
    grouped = defaultdict(set)
    for fact in merged:
        grouped[(fact["metric_id"], fact["year"])].add(Decimal(fact["value"]))
    for fact in merged:
        fact["status"] = "conflict" if len(grouped[(fact["metric_id"], fact["year"])]) > 1 else "candidate"
    return merged, removed


def check_reported_measures(candidates, measures):
    """Only accept reported net debt when the report's debt/cash bridge ties."""
    by_key = defaultdict(set)
    for fact in candidates:
        by_key[(fact["metric_id"], fact["year"])].add(Decimal(fact["value"]))
    checked = []
    for measure in measures:
        year = measure["year"]
        required = ("balance_sheet_32", "balance_sheet_39", "balance_sheet_33",
                    "balance_sheet_40", "balance_sheet_8")
        values = [by_key[(metric, year)] for metric in required]
        if any(len(v) != 1 for v in values):
            measure["status"] = "unverified"
            continue
        components = [next(iter(v)) for v in values]
        calculated = sum(components[:-1]) - components[-1]
        if abs(calculated - Decimal(measure["value"])) > Decimal("0.001"):
            measure["status"] = "conflict"
            continue
        measure["status"] = "reconciled"
        measure["reconciliation"] = {"formula": "current debt + non-current debt + current leases + non-current leases − cash",
                                      "calculated": str(calculated), "reported": measure["value"]}
        checked.append(measure)
    return checked


def kpi_coverage(candidates, settings, reported_measures=()):
    """Calculate sourced KPIs and explain every unavailable dependency.

    An alternative ratio keeps the report's own definition in its label. A
    reported debt measure must reconcile against the SAME selected inputs.
    """
    grouped = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate["metric_id"], candidate["year"])].append(candidate)
    # Dependencies and displayed definitions are deliberately explicit. An
    # unavailable liquid-investment balance is not interpreted as zero.
    definitions = (
        ("quick_liquid", "Quick ratio (liquid assets)", "Άμεση ρευστότητα (ρευστά στοιχεία)",
         ["balance_sheet_8", "balance_sheet_9", "balance_sheet_10", "balance_sheet_37"],
         "(cash + liquid investments + net trade receivables) / current liabilities",
         lambda v: (v[0] + v[1] + v[2]) / v[3]),
        ("quick_ex_inventory", "Quick ratio (inventory exclusion)", "Άμεση ρευστότητα (χωρίς αποθέματα)",
         ["balance_sheet_17", "balance_sheet_11", "balance_sheet_37"],
         "(current assets - inventories) / current liabilities",
         lambda v: (v[0] - v[1]) / v[2]),
        ("ebitda", "EBITDA, calculated", "EBITDA, υπολογισμένο",
         ["income_statement_16", "income_statement_17"], "EBIT + depreciation and amortisation",
         lambda v: v[0] + v[1]),
        ("interest_coverage", "EBIT / interest expense", "EBIT / έξοδα τόκων",
         ["income_statement_16", "income_statement_22"], "EBIT / interest expense",
         lambda v: v[0] / v[1]),
        ("ebitda_coverage", "Calculated EBITDA / interest expense", "Υπολογισμένο EBITDA / έξοδα τόκων",
         ["income_statement_16", "income_statement_17", "income_statement_22"],
         "(EBIT + depreciation and amortisation) / interest expense",
         lambda v: (v[0] + v[1]) / v[2]),
        ("net_debt", "Net debt (cash and liquid investments deducted)",
         "Καθαρός δανεισμός (με αφαίρεση ρευστών επενδύσεων)",
         ["balance_sheet_32", "balance_sheet_39", "balance_sheet_33", "balance_sheet_40",
          "balance_sheet_8", "balance_sheet_9"],
         "current and long-term borrowings + leases - cash - liquid investments",
         lambda v: sum(v[:4]) - v[4] - v[5]),
        ("roic", "ROIC, normalised tax / average cash-adjusted capital",
         "ROIC, κανονικοποιημένος φόρος / μέσο προσαρμοσμένο κεφάλαιο",
         ["income_statement_16", "market_inputs_17"] +
         [f"{m}@current" for m in ("balance_sheet_32", "balance_sheet_39", "balance_sheet_33",
                                    "balance_sheet_40", "balance_sheet_52", "balance_sheet_8", "balance_sheet_9")] +
         [f"{m}@prior" for m in ("balance_sheet_32", "balance_sheet_39", "balance_sheet_33",
                                  "balance_sheet_40", "balance_sheet_52", "balance_sheet_8", "balance_sheet_9")],
         "EBIT × (1 - analyst normalised tax) / average(debt + equity - cash - liquid investments)",
         lambda v: v[0] * (1 - v[1]) / ((sum(v[2:6]) + v[6] - v[7] - v[8] +
                                        sum(v[9:13]) + v[13] - v[14] - v[15]) / 2)),
        ("pe", "Year-end price / reported basic EPS", "Τιμή τέλους χρήσης / δημοσιευμένο βασικό EPS",
         ["market_inputs_7", "market_inputs_10"], "year-end closing price / reported basic EPS",
         lambda v: v[0] / v[1]),
        ("reported_net_debt_ebitda", "Reported net debt / reported EBITDA (issuer definition)",
         "Δημοσιευμένος καθαρός δανεισμός / δημοσιευμένο EBITDA (ορισμός εκδότη)",
         ["reported_net_debt", "income_statement_18"], "reconciled reported net debt / reported EBITDA",
         lambda v: v[0] / v[1]),
    )
    from .engine import METRICS
    results = []
    for year in range(settings.latest_year - 5, settings.latest_year + 1):
        for identifier, en, el, dependencies, formula, calculate in definitions:
            missing, ambiguous, inputs = [], [], []
            for dependency in dependencies:
                metric, _, period = dependency.partition("@")
                dependency_year = year - 1 if period == "prior" else year
                group = ([r for r in reported_measures if r["metric_id"] == metric and r["year"] == dependency_year
                          and r.get("status") == "reconciled"] if metric == "reported_net_debt"
                         else grouped.get((metric, dependency_year), []))
                values = {Decimal(c["value"]) for c in group}
                display = ("Reported net debt" if metric == "reported_net_debt"
                           else METRICS[metric]["label"][settings.language])
                if not values:
                    missing.append({"metric_id": metric, "year": dependency_year, "label": display,
                                    "reason": "analyst assumption" if metric != "reported_net_debt" and
                                    not METRICS[metric]["automatic"] else "no supported source"})
                elif len(values) > 1:
                    ambiguous.append({"metric_id": metric, "year": dependency_year, "label": display,
                                      "reason": "conflicting sources"})
                else:
                    inputs.append({"metric_id": metric, "year": dependency_year,
                                   "value": str(next(iter(values))),
                                   "sources": [{"file": c.get("file"), "page": c.get("page"),
                                                "document_id": c.get("document_id"),
                                                "row_label": c.get("row_label", ""),
                                                "quote": c.get("quote", "")}
                                               for c in group if Decimal(c["value"]) in values]})
            value = None
            if not missing and not ambiguous:
                values = [Decimal(item["value"]) for item in inputs]
                # Guard every divisor and reject nonsensical invested capital.
                try:
                    computed = calculate(values)
                    if computed.is_finite() and (identifier != "roic" or (0 <= values[1] <= 1 and
                            (sum(values[2:6]) + values[6] - values[7] - values[8] +
                             sum(values[9:13]) + values[13] - values[14] - values[15]) > 0)):
                        value = str(computed)
                except (ZeroDivisionError, OverflowError):
                    pass
            eps_continuing = identifier == "pe" and any(
                "from continued operations" in source.get("row_label", "").casefold()
                for operand in inputs if operand["metric_id"] == "market_inputs_10"
                for source in operand["sources"])
            label = (("P/E (reported basic EPS from continuing operations)" if settings.language == "en"
                      else "P/E (δημοσιευμένο βασικό EPS από συνεχιζόμενες δραστηριότητες)")
                     if eps_continuing else el if settings.language == "el" else en)
            results.append({"id": identifier, "year": year, "label": label,
                            "status": "ambiguous" if ambiguous else "missing" if missing else
                                      "available" if value is not None else "invalid_denominator",
                            "value": value, "formula": formula,
                            "unit": "money" if identifier in ("net_debt", "ebitda") else "ratio",
                            "inputs": inputs, "missing": missing, "ambiguous": ambiguous})
    return results
