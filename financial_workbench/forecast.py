"""Transparent driver-based operating scenarios, never a security-price forecast."""

from decimal import Decimal


def operating_scenarios(decisions, settings, shock_pp=Decimal("5"), baseline_override=None):
    """Project revenue and EBIT from reviewed history or an explicit assumption.

    The median of annual growth rates damps one extreme year. Fewer than three
    consecutive, nonconflicting years requires an analyst-supplied rate. No
    extrapolation is represented as a probability or a valuation conclusion.
    """
    shock = Decimal(str(shock_pp))
    if not 0 <= shock <= 20:
        raise ValueError("Scenario spread must be between 0 and 20 percentage points")
    override = None if baseline_override is None else Decimal(str(baseline_override))
    if override is not None and not Decimal("-0.9") <= override <= Decimal("1"):
        raise ValueError("Assumed annual growth must be between -90% and 100%")
    by_key = {}
    for item in decisions:
        by_key.setdefault((item["metric_id"], item["year"]), []).append(item)

    def unique(metric, year):
        group = by_key.get((metric, year), [])
        values = {Decimal(item["value"]) for item in group}
        return group[0] if len(values) == 1 else None

    last = settings.latest_year
    sources = [unique("income_statement_8", year) for year in range(last - 5, last + 1)]
    anchor = sources[-1]
    result = {"status": "insufficient_history", "latest_year": last, "history": [],
              "method": "Median historical annual revenue growth; EBIT at latest reported operating margin",
              "shock_pp": str(shock), "baseline_growth": None, "scenario_rows": [],
              "assumption_source": "analyst" if override is not None else "historical data"}
    for year, fact in zip(range(last - 5, last + 1), sources):
        if fact is not None:
            result["history"].append({"year": year, "revenue": fact["value"],
                                      "document_id": fact.get("document_id"),
                                      "file": fact.get("file"), "page": fact.get("page")})
    if anchor is None or Decimal(anchor["value"]) <= 0:
        result["reason"] = "A positive, sourced revenue value for the latest year is required."
        return result
    rates = []
    for previous, current in zip(sources, sources[1:]):
        if previous is None or current is None:
            rates.append(None)
        else:
            prior, now = Decimal(previous["value"]), Decimal(current["value"])
            rates.append(now / prior - 1 if prior > 0 and now > 0 else None)
    consecutive = []
    for rate in reversed(rates):
        if rate is None:
            break
        consecutive.append(rate)
    if override is None and len(consecutive) < 2:
        result["reason"] = "Three consecutive years of compatible, unconflicted revenue or an explicit analyst growth rate are required."
        return result
    if override is None:
        ordered = sorted(consecutive)
        mid = len(ordered) // 2
        baseline = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    else:
        baseline = override
    if baseline <= -1 or baseline + shock / 100 <= -1:
        result["reason"] = "The historical contraction is too steep for this simple compounding model."
        return result
    operating = unique("income_statement_16", last)
    margin = (Decimal(operating["value"]) / Decimal(anchor["value"])
              if operating else None)
    result.update(status="available", baseline_growth=str(baseline),
                  operating_margin=str(margin) if margin is not None else None,
                  historical_growth=[str(x) for x in reversed(consecutive)])
    for name, growth in (("downside", baseline - shock / 100),
                         ("base", baseline), ("upside", baseline + shock / 100)):
        for offset in (1, 2, 3):
            revenue = Decimal(anchor["value"]) * (1 + growth) ** offset
            result["scenario_rows"].append({"name": name, "year": last + offset,
                                            "growth": str(growth),
                                            "revenue": str(revenue.quantize(Decimal("0.0001"))),
                                            "ebit": str((revenue * margin).quantize(Decimal("0.0001")))
                                            if margin is not None else None})
    return result
