"""XBRL company facts: the numbers, as filed.

The trap here is that "revenue" is not one tag. Apple reports
RevenueFromContractWithCustomerExcludingAssessedTax; older filers use Revenues;
some use SalesRevenueNet. A tool that only knows one tag returns "no data" for
most of the market and looks broken.

So every friendly concept name maps to a list of candidate tags, tried in order.
The response says which tag actually answered, because in finance the definition
matters as much as the number.
"""

from __future__ import annotations

from .client import EdgarError, fetch, pad_cik

CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet",
    ],
    "net_income":      ["NetIncomeLoss", "ProfitLoss"],
    "operating_income":["OperatingIncomeLoss"],
    "gross_profit":    ["GrossProfit"],
    "total_assets":    ["Assets"],
    "total_liabilities":["Liabilities"],
    "stockholders_equity": ["StockholdersEquity",
                            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "eps_diluted":     ["EarningsPerShareDiluted"],
    "eps_basic":       ["EarningsPerShareBasic"],
    "rnd_expense":     ["ResearchAndDevelopmentExpense"],
    "shares_outstanding": ["CommonStockSharesOutstanding", "dei:EntityCommonStockSharesOutstanding"],
    "long_term_debt":  ["LongTermDebtNoncurrent", "LongTermDebt"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
}


def _annual_only(rows: list[dict]) -> list[dict]:
    """Keep full-year / point-in-time figures and drop quarterly noise."""
    keep = []
    for r in rows:
        if r.get("form") not in ("10-K", "20-F"):
            continue
        start, end = r.get("start"), r.get("end")
        if start and end:
            days = (_d(end) - _d(start)).days
            if not 330 <= days <= 400:      # a fiscal year, allowing odd calendars
                continue
        keep.append(r)
    return keep


def _d(s: str):
    from datetime import date
    y, m, dd = (int(x) for x in s.split("-"))
    return date(y, m, dd)


def get_facts(cik: str, concept: str, *, periods: int = 8,
              annual: bool = True) -> dict:
    cik10 = pad_cik(cik)
    key = concept.strip().lower().replace(" ", "_").replace("-", "_")
    if key not in CONCEPTS:
        raise EdgarError(
            f"Unknown concept {concept!r}. Available: {', '.join(sorted(CONCEPTS))}. "
            "These are friendly names; each maps to one or more US-GAAP XBRL tags."
        )

    # Filers switch tags mid-life: NVIDIA's older years sit under one revenue tag
    # and its recent years under another. Stopping at the first tag that returns
    # anything silently serves stale numbers, so collect every tag and merge.
    tried, per_tag, unit = [], [], None
    for tag in CONCEPTS[key]:
        tried.append(tag)
        try:
            data = fetch(CONCEPT_URL.format(cik=cik10, tag=tag))
        except EdgarError:
            continue                          # 404 just means this filer never used it
        units = data.get("units") or {}
        if not units:
            continue
        unit = unit or next(iter(units))
        picked = _annual_only(units[next(iter(units))]) if annual \
            else units[next(iter(units))]
        if picked:
            per_tag.append((tag, picked, max(r["end"] for r in picked)))

    # Newest-reporting tag leads; the others only fill in periods it lacks.
    per_tag.sort(key=lambda t: t[2], reverse=True)
    rows, used_tag, covered = [], None, set()
    tags_used = []
    for tag, picked, _newest in per_tag:
        added = [r for r in picked if r["end"] not in covered]
        if not added:
            continue
        used_tag = used_tag or tag
        tags_used.append(tag)
        covered.update(r["end"] for r in added)
        rows.extend(added)

    if not rows:
        raise EdgarError(
            f"CIK {cik10} reports no values for {concept!r}. Tags tried: "
            f"{', '.join(tried)}. Some filers use an industry-specific tag, and "
            "foreign private issuers often file under IFRS rather than US-GAAP."
        )

    # Newest first, one row per period end.
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["end"], reverse=True):
        if r["end"] in seen:
            continue
        seen.add(r["end"])
        out.append({
            "period_end": r["end"],
            "period_start": r.get("start"),
            "value": r["val"],
            "fiscal_year": r.get("fy"),
            "fiscal_period": r.get("fp"),
            "form": r.get("form"),
            "filed": r.get("filed"),
        })
        if len(out) >= periods:
            break

    return {
        "cik": cik10,
        "concept": key,
        "xbrl_tag": used_tag,
        "all_tags_used": tags_used if len(tags_used) > 1 else None,
        "unit": unit,
        "basis": "annual" if annual else "as-reported (all periods)",
        "periods": out,
    }
