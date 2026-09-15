"""The MCP server: six tools over SEC EDGAR.

Tool design notes, since that is most of the work here:

- **Six tools, not thirty.** EDGAR exposes dozens of endpoints. A model does not
  need them; it needs to find a company, see what it filed, read one part of one
  filing, and look up a number. Every extra tool is context spent before the
  first useful call.

- **Accept what a model actually has.** Every tool takes `company` as plain text —
  "Apple", "AAPL", or a CIK — and resolves it internally. Forcing a find_company
  round-trip before every other call wastes a turn on a problem the server can
  solve itself.

- **Never return a whole 10-K.** `read_filing_section` is the core tool precisely
  because it refuses to. It returns one item, budgeted, with an offset to
  continue. Returning 300 pages is not "being helpful", it is destroying the
  context window the answer has to fit in.

- **Errors are instructions.** Every failure says what to call next. An ambiguous
  company name comes back with the candidates; a missing section comes back
  pointing at list_sections.
"""

from __future__ import annotations

from typing import Any

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import budget, companies, facts, filings, search, sections
from .client import EdgarError

server = MCPServer(
    name="edgar",
    instructions=(
        "Read SEC filings from EDGAR: US public company disclosures, 2001 to "
        "present. Typical flow: find_company to get a CIK, list_filings to find "
        "the filing, then read_filing_section for the part you need. For numbers, "
        "skip straight to get_financial_facts — it reads structured XBRL data and "
        "is far more reliable than parsing them out of prose. Everything here is "
        "public record; nothing is investment advice."
    ),
)


def expected(fn):
    """Surface our own error text to the model.

    The SDK treats an unrecognised exception as a crash and replaces the message
    with "Error executing tool <name>". Every recovery instruction we wrote would
    be thrown away. ToolError is the anticipated-failure type, and its message is
    delivered to the caller intact.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EdgarError as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def _cik(company: str) -> tuple[str, str]:
    """Accept a ticker, a name, or a CIK. Returns (cik, display name)."""
    q = company.strip()
    if q.isdigit() or (q.upper().startswith("CIK") and q[3:].strip().isdigit()):
        digits = q.upper().replace("CIK", "").strip()
        return digits.zfill(10), f"CIK {digits.zfill(10)}"
    hit = companies.resolve(q)
    return hit["cik"], hit["name"]


@server.tool(
    description=(
        "Resolve a company name or stock ticker to its SEC CIK number, which every "
        "other tool needs. Use this when a name is ambiguous or you want to confirm "
        "which entity you are about to read. Only companies that file with the SEC "
        "are here: no private companies, and most foreign issuers only if they file "
        "a 20-F. Returns ranked candidates with a confidence score."
    )
)
@expected
def find_company(query: str, limit: int = 5) -> dict[str, Any]:
    hits = companies.search(query, limit=limit)
    if not hits:
        raise EdgarError(
            f"No SEC registrant matched {query!r}. Try the stock ticker or the "
            "exact legal name. Private companies do not appear here."
        )
    return {"query": query, "matches": hits}


@server.tool(
    description=(
        "List what a company has filed with the SEC, newest first. Filter by form "
        "type: 10-K is the annual report, 10-Q quarterly, 8-K a material event, "
        "DEF 14A the proxy statement (executive pay and board), S-1 an IPO "
        "registration, 20-F a foreign issuer's annual report, 4 an insider trade. "
        "The form filter matches on prefix, so '10-K' also returns 10-K/A "
        "amendments. Returns accession numbers, which read_filing_section needs."
    )
)
@expected
def list_filings(company: str, form: str | None = None, limit: int = 10,
                 since: str | None = None) -> dict[str, Any]:
    cik, _ = _cik(company)
    return filings.list_filings(cik, form=form, limit=limit, since=since)


@server.tool(
    description=(
        "Show which numbered items a filing actually contains, and how many tokens "
        "each one would cost to read. Call this before read_filing_section when you "
        "are not sure a section exists or want to budget the read. Many companies "
        "incorporate Part III by reference to their proxy, so Items 10-14 are often "
        "a single sentence pointing elsewhere."
    )
)
@expected
def list_filing_sections(company: str, accession: str) -> dict[str, Any]:
    cik, name = _cik(company)
    meta = filings.find_filing(cik, accession)
    text = sections.load_document_text(meta["url"])
    found = sections.list_sections(text)
    return {
        "company": meta["company"],
        "form": meta["form"],
        "filed": meta["filed"],
        "accession": meta["accession"],
        "document_tokens": budget.estimate_tokens(text),
        "sections": found,
        "note": "Read one section with read_filing_section. Reading the whole "
                f"document would cost about {budget.estimate_tokens(text):,} tokens.",
    }


@server.tool(
    description=(
        "Read ONE section of a filing rather than the whole document. This is the "
        "main tool: a 10-K is typically 50,000+ tokens and you almost never need "
        "all of it.\n\n"
        "Sections: business (Item 1, what the company does), risk_factors (1A, what "
        "management says could go wrong — the most quoted section), properties (2), "
        "legal_proceedings (3), market_for_stock (5), mdna (7, management's own "
        "explanation of the results — read this before the financial statements), "
        "market_risk (7A), financial_statements (8), controls (9A), directors (10), "
        "executive_comp (11).\n\n"
        "Output is capped at max_tokens. If the section is longer you get an offset "
        "to continue from; the response always says how much was cut."
    )
)
@expected
def read_filing_section(company: str, accession: str, section: str,
                        max_tokens: int = 6000, offset: int = 0) -> dict[str, Any]:
    cik, _ = _cik(company)
    meta = filings.find_filing(cik, accession)
    text = sections.load_document_text(meta["url"])
    label, body = sections.extract_section(text, section)
    window = budget.clip(
        body, max_tokens=max_tokens, offset=offset,
        more_hint="Call read_filing_section again with the same arguments.",
    )
    return {
        "company": meta["company"],
        "form": meta["form"],
        "filed": meta["filed"],
        "period": meta["period"],
        "accession": meta["accession"],
        "section": section,
        "heading": label,
        "source_url": meta["url"],
        **window,
    }


@server.tool(
    description=(
        "Look up a reported financial figure across recent periods, from structured "
        "XBRL data. Use this for any number — it is far more reliable than reading "
        "figures out of filing prose.\n\n"
        "Concepts: revenue, net_income, operating_income, gross_profit, "
        "total_assets, total_liabilities, stockholders_equity, cash, eps_basic, "
        "eps_diluted, rnd_expense, shares_outstanding, long_term_debt, "
        "operating_cash_flow.\n\n"
        "Each maps to one or more US-GAAP tags and the response names the tag that "
        "answered, because the exact definition matters. Annual figures by default; "
        "set annual=false for every period as reported, including quarters."
    )
)
@expected
def get_financial_facts(company: str, concept: str, periods: int = 8,
                        annual: bool = True) -> dict[str, Any]:
    cik, name = _cik(company)
    out = facts.get_facts(cik, concept, periods=periods, annual=annual)
    out["company"] = name
    return out


@server.tool(
    description=(
        "Search the full text of all EDGAR filings from 2001 onward. Use this to "
        "find WHO discusses something — 'which companies mention quantum computing "
        "risk' — rather than to read a company you have already identified.\n\n"
        "A multi-word query is matched as an exact phrase. Results are filings, not "
        "passages, so follow up with read_filing_section to read one. Narrow with "
        "forms (e.g. '10-K') and a date range when a query is common."
    )
)
@expected
def search_filings(query: str, forms: str | None = None, limit: int = 10,
                   date_from: str | None = None,
                   date_to: str | None = None) -> dict[str, Any]:
    return search.full_text(query, forms=forms, limit=limit,
                            date_from=date_from, date_to=date_to)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
