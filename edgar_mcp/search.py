"""EDGAR full-text search — the only way to ask "who talks about X".

Covers 2001 to present. Returns filing-level hits, not passages, so the intended
flow is: search to find the filings, then read_filing_section to read one.
"""

from __future__ import annotations

from .client import EdgarError, fetch
from urllib.parse import quote

FTS = "https://efts.sec.gov/LATEST/search-index?q={q}"


def full_text(query: str, *, forms: str | None = None, date_from: str | None = None,
              date_to: str | None = None, limit: int = 10) -> dict:
    q = query.strip()
    if not q:
        raise EdgarError("Search query is empty.")

    url = FTS.format(q=quote(f'"{q}"' if " " in q and '"' not in q else q))
    if forms:
        url += f"&forms={quote(forms)}"
    if date_from and date_to:
        url += f"&dateRange=custom&startdt={date_from}&enddt={date_to}"

    data = fetch(url)
    hits = (data.get("hits") or {}).get("hits") or []
    total = ((data.get("hits") or {}).get("total") or {}).get("value", 0)

    rows = []
    for h in hits[:limit]:
        src = h.get("_source") or {}
        acc, _, doc = (h.get("_id") or "").partition(":")
        names = src.get("display_names") or []
        rows.append({
            "company": names[0] if names else None,
            "cik": (src.get("ciks") or [None])[0],
            "form": src.get("root_form") or src.get("file_type"),
            "filed": src.get("file_date"),
            "accession": acc,
            "document": doc,
            "score": round(h.get("_score", 0), 1),
        })

    if not rows:
        raise EdgarError(
            f"No filings contain {query!r}. Full-text search covers 2001 onward and "
            "matches exact phrases. Try fewer words, or drop the form filter."
        )

    return {
        "query": q,
        "total_matches": total,
        "returned": len(rows),
        "note": "Filing-level hits. Use read_filing_section to read one."
                if total > len(rows) else None,
        "results": rows,
    }
