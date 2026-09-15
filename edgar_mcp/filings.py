"""Listing what a company has filed, and locating the document behind a filing."""

from __future__ import annotations

from .client import EdgarError, fetch, pad_cik

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# Ordered so the common asks are obvious to a model reading the tool description.
COMMON_FORMS = ["10-K", "10-Q", "8-K", "DEF 14A", "S-1", "20-F", "13F-HR", "4"]


def list_filings(cik: str, *, form: str | None = None, limit: int = 10,
                 since: str | None = None) -> dict:
    """Recent filings for a company, newest first.

    `form` matches on a prefix, so "10-K" also returns "10-K/A" amendments.
    """
    cik10 = pad_cik(cik)
    data = fetch(SUBMISSIONS.format(cik=cik10))
    recent = data.get("filings", {}).get("recent", {})
    if not recent.get("accessionNumber"):
        raise EdgarError(f"CIK {cik10} has no filings on record.")

    wanted = form.upper().strip() if form else None
    rows = []
    for i in range(len(recent["accessionNumber"])):
        f = recent["form"][i]
        if wanted and not f.upper().startswith(wanted):
            continue
        filed = recent["filingDate"][i]
        if since and filed < since:
            continue
        rows.append({
            "form": f,
            "filed": filed,
            "period": recent["reportDate"][i] or None,
            "accession": recent["accessionNumber"][i],
            "primary_document": recent["primaryDocument"][i],
            "description": recent["primaryDocDescription"][i] or None,
        })
        if len(rows) >= limit:
            break

    if not rows:
        available = sorted({recent["form"][i] for i in range(len(recent["form"]))})[:20]
        raise EdgarError(
            f"{data.get('name', cik10)} has no {form or 'matching'} filings"
            f"{' since ' + since if since else ''}. Forms actually on file include: "
            f"{', '.join(available)}."
        )

    return {
        "company": data.get("name"),
        "cik": cik10,
        "tickers": data.get("tickers", []),
        "industry": data.get("sicDescription"),
        "filings": rows,
    }


def document_url(cik: str, accession: str, document: str) -> str:
    return ARCHIVE.format(
        cik=str(int(pad_cik(cik))),
        acc=accession.replace("-", ""),
        doc=document,
    )


def find_filing(cik: str, accession: str) -> dict:
    """Look one filing up by accession number so we can locate its main document."""
    cik10 = pad_cik(cik)
    data = fetch(SUBMISSIONS.format(cik=cik10))
    recent = data.get("filings", {}).get("recent", {})
    target = accession.strip()
    flat = target.replace("-", "")
    for i, acc in enumerate(recent.get("accessionNumber", [])):
        if acc == target or acc.replace("-", "") == flat:
            return {
                "company": data.get("name"),
                "cik": cik10,
                "form": recent["form"][i],
                "filed": recent["filingDate"][i],
                "period": recent["reportDate"][i] or None,
                "accession": acc,
                "primary_document": recent["primaryDocument"][i],
                "url": document_url(cik10, acc, recent["primaryDocument"][i]),
            }
    raise EdgarError(
        f"Accession {accession} is not among the 1,000 most recent filings for "
        f"CIK {cik10}. Call list_filings first and use an accession from its result."
    )
