"""An auditable, bounded search plan for unresolved financial inputs.

The search examines the entire job-specific index, including all pages, notes
and statement rows. A search failure is never described as proof of absence.
"""

from collections import defaultdict

from .engine import METRICS


QUERIES = {
    "balance_sheet_9": ("liquid investments", "short term investments", "marketable securities", "cash equivalents"),
    "balance_sheet_10": ("trade receivables", "accounts receivable", "loss allowance"),
    "income_statement_17": ("depreciation amortisation", "depreciation amortization", "right of use depreciation"),
    "income_statement_18": ("reported EBITDA", "adjusted EBITDA", "earnings before interest"),
    "income_statement_22": ("interest expense", "interest on borrowings", "finance costs"),
    "market_inputs_7": ("closing share price", "year end share price", "December share price"),
    "market_inputs_8": ("shares outstanding", "treasury shares", "issued share capital"),
    "market_inputs_9": ("weighted average shares", "ordinary shares basic", "earnings per share"),
    "market_inputs_10": ("basic earnings per share", "basic EPS"),
}


def investigate_gaps(pages, coverage, settings, search):
    """Return source leads for unmet, automatic KPI inputs with an audit trail.

    Search one unique metric across all years, then inspect each hit's dated
    columns. This bounds searches for six-year workbooks without dropping any
    PDF page from the indexed corpus.
    """
    missing = defaultdict(set)
    for item in coverage:
        for dependency in item["missing"] + item["ambiguous"]:
            metric = dependency["metric_id"]
            if metric in METRICS and METRICS[metric]["automatic"]:
                missing[metric].add(dependency["year"])
    lookup = {(p["sha256"], p["page"], side["side"]): side
              for p in pages for side in p.get("panels", [])}
    entries = []
    unreadable = sum(len((p.get("text") or "").strip()) < 15 for p in pages)
    for metric, years in sorted(missing.items()):
        english = QUERIES.get(metric, (METRICS[metric]["label"]["en"],))
        # Search both disclosure languages irrespective of the workbook UI.
        terms = tuple(dict.fromkeys((*english[:3], METRICS[metric]["label"]["el"])))
        hits = {}
        def add_results(query, results):
            for result in results:
                panel = lookup.get((result["document_id"], result["page"], result["side"]))
                if panel is None:
                    continue
                row_years = {h["year"] for h in panel.get("headers", []) + panel.get("note_headers", [])}
                # A narrative page may mention a year without a tabular date
                # header. Surface it for human review; never extract a number
                # from it as a verified year/metric pair.
                if not row_years:
                    row_years = {year for year in years if str(year) in panel.get("text", "")}
                if not years.intersection(row_years):
                    continue
                key = result["document_id"], result["page"], result["side"]
                lead = hits.setdefault(key, {"file": result["file"], "document_id": result["document_id"],
                                             "page": result["page"], "side": result["side"],
                                             "years": sorted(years.intersection(row_years)),
                                             "kind": result["kind"], "text": result["text"][:600],
                                             "queries": []})
                if query not in lead["queries"]:
                    lead["queries"].append(query)
        for query in terms:
            add_results(query, search(query, limit=30, all_terms=True))
        # An older comparative year may rank below common current-year text.
        # Narrow those searches by year instead of discarding later pages.
        for year in years - {yr for lead in hits.values() for yr in lead["years"]}:
            for query in terms:
                add_results(query, search(f"{query} {year}", limit=30, all_terms=True))
        for year in sorted(years):
            leads = sorted((lead for lead in hits.values() if year in lead["years"]),
                           key=lambda h: (-len(h["queries"]), h["kind"] == "panel", h["page"]))[:6]
            entries.append({"metric_id": metric, "year": year,
                            "label": METRICS[metric]["label"][settings.language],
                            "state": "related_evidence_needs_review" if leads else
                                     "low_text_pages_need_review" if unreadable else
                                     "no_supported_match_after_full_index_search",
                            "searched_pages": len(pages), "unreadable_pages": unreadable,
                            "queries": list(terms), "leads": leads})
    return entries
