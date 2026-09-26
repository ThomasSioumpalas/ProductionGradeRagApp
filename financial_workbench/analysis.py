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

from .engine import (_candidates_from_row, _label, _meaning_allowed, _separator,
                     model_mapping_rejection, parse_number)


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
        aktor_interest = ("bank and bond loans", "lease liabilities",
                          "interest on advances from customers",
                          "interest on borrowings from third parties")
        if all(names.count(name) == 1 for name in aktor_interest):
            # This source explicitly separates interest-bearing items from
            # letters of credit, FX, discount unwind and other finance fees.
            selected = [rows[names.index(name)] for name in aktor_interest]
            for year in range(settings.latest_year - 5, settings.latest_year + 1):
                values = [_amount(row, settings.scope, year, settings) for row in selected]
                if any(value is None for value in values):
                    continue
                if any(value > 0 for value in values):
                    continue
                yield _derived("income_statement_22", selected, settings, year,
                               -sum(values),
                               "Bank/bond + lease + customer-advance + third-party interest")
            continue
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


def _note_total(rows, start, settings, year):
    """The printed "Total" of the note table that begins at ``start``, if any."""
    total = next((r for r in rows[start:] if _label(r["label"]) == "total"), None)
    column = total and next((v for v in total["values"]
                             if v["year"] == year and v["scope"] == settings.scope), None)
    if not column:
        return None
    return {"raw": column["raw_value"], "value": str(_dash_amount(total, settings, year)),
            "quote": total["quote"], "page": total["page"]}


def _dash_amount(row, settings, year):
    column = next((v for v in row["values"] if v["year"] == year and v["scope"] == settings.scope), None)
    if not column:
        return None
    if column["raw_value"].strip() in ("-", "–", "—"):
        return Decimal(0)
    return _amount(row, settings.scope, year, settings)


def _trade_payables(panels, settings):
    """Trade payables from a trade-and-other-payables note whose lines tie to its total.

    Trade lines are those named trade payables / suppliers, each with an
    immediately following "related parties" sub-line. Every line above the
    printed total must add up to it, or nothing is derived.
    """
    for _panel, rows in _source_rows(panels, "payable"):
        names = [_label(r["label"]) for r in rows]
        if "total" not in names:
            continue
        end = names.index("total")
        trade = []
        for i, name in enumerate(names[:end]):
            if name.startswith("trade payable") or name in ("suppliers", "trade creditors"):
                trade.append(rows[i])
                if i + 1 < end and names[i + 1] == "related parties":
                    trade.append(rows[i + 1])
        if not trade:
            continue
        for year in range(settings.latest_year - 5, settings.latest_year + 1):
            body = [_dash_amount(r, settings, year) for r in rows[:end]]
            total = _dash_amount(rows[end], settings, year)
            if total is None or any(v is None for v in body) or sum(body) != total:
                continue
            value = sum(_dash_amount(r, settings, year) for r in trade)
            fact = _derived("balance_sheet_31", trade, settings, year, value,
                            " + ".join(r["label"] for r in trade),
                            check={"label": "Note lines = printed note total",
                                   "reported": str(total), "calculated": str(sum(body)),
                                   "source": rows[end]["quote"]})
            fact["note_total"] = _note_total(rows, 0, settings, year)
            yield fact


def _trade_receivables(panels, settings):
    for _panel, rows in _source_rows(panels, "trade and other receivables"):
        names = [_label(r["label"]) for r in rows]
        for i in range(len(rows) - 3):
            if names[i:i + 4] != ["trade receivables", "trade receivables related parties",
                                    "less impairment provisions", "final trade receivables"]:
                continue
            components, reported = rows[i:i + 3], rows[i + 3]
            for year in range(settings.latest_year - 5, settings.latest_year + 1):
                values = [_amount(row, settings.scope, year, settings) for row in components]
                subtotal = _amount(reported, settings.scope, year, settings)
                if subtotal is None or any(value is None for value in values):
                    continue
                if sum(values) != subtotal:
                    continue
                fact = _derived("balance_sheet_10", components + [reported], settings,
                                year, subtotal,
                                "Gross trade + related-party trade − impairment",
                                check={"label": "Net trade receivables = printed final total",
                                       "reported": str(subtotal),
                                       "calculated": str(sum(values)),
                                       "source": reported["quote"]})
                fact["note_total"] = _note_total(rows, i, settings, year)
                yield fact
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
                fact = _derived("balance_sheet_10", components + [subtotal], settings, year,
                                reported, "Gross trade receivables − loss allowance + trade related parties",
                                check={"label": "Component sum = printed trade subtotal",
                                       "reported": str(reported), "calculated": str(sum(vals)),
                                       "source": subtotal["quote"]})
                fact["note_total"] = _note_total(rows, i, settings, year)
                yield fact


def _weighted_shares(panels, pages, settings):
    by_page = {(p["sha256"], p["page"]): p for p in pages}
    rejected = []
    for _panel, rows in list(_source_rows(panels, "earnings per share")) + list(_source_rows(panels, "profit losses per share")):
        for row in rows:
            if _label(row["label"]) not in ("weighted average number of ordinary shares for the purposes of basic earnings per share",
                                         "weighted average number of shares"):
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
                elif label in ("basic earnings per share", "basic earnings per share eur",
                               "basic profit losses per share") and (
                        "earnings per share" in title or "profit losses per share" in title):
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


def _is_cash_flow_row(pages, fact):
    page = next((p for p in pages if p["sha256"] == fact.get("document_id")
                 and p["page"] == fact.get("page")), None)
    return bool(page) and any(panel.get("statement") == "cash" and panel["side"] == fact.get("panel")
                              for panel in page["panels"])


def enrich_financial_evidence(pages, settings, search=None):
    """Query the persistent page index and validate only matched note panels."""
    cf = list(_cf_depreciation(pages, settings))
    finance = list(_finance_cost(_panels_for(pages, search, "finance cost"), settings))
    trade = list(_trade_receivables(_panels_for(pages, search, "trade receivables"), settings))
    trade += list(_trade_payables(_panels_for(pages, search, "trade payables"), settings))
    shares = list(_weighted_shares(_panels_for(pages, search, "weighted average ordinary shares"), pages, settings))
    direct = list(_direct_note_facts(pages, settings))
    # An itemised finance-cost note separates interest from letters-of-credit
    # fees, discounting and FX. It is preferred to a cash-flow add-back merely
    # labelled "interest expense" for the same year and report.
    bridged = {(f["year"], f["document_id"]): f for f in finance
               if f["metric_id"] == "income_statement_22"}
    for fact in [f for f in direct if f["metric_id"] == "income_statement_22"
                 and (f["year"], f["document_id"]) in bridged and f.get("section", "") != "note"
                 and _is_cash_flow_row(pages, f)]:
        direct.remove(fact)
        bridged[(fact["year"], fact["document_id"])].setdefault("superseded", []).append(
            {"file": fact["file"], "page": fact["page"], "label": fact.get("row_label", ""),
             "value": fact["value"],
             "reason": "Cash-flow add-back; the itemised finance-cost note separates interest"})
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
                and (not _meaning_allowed({"label": fact.get("row_label", ""),
                                           "section": fact.get("section", "")}, fact["metric_id"])
                     or model_mapping_rejection(fact.get("row_label", ""), fact["metric_id"]))):
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
