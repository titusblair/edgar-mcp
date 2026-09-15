"""Resolving a human's words to a CIK, which is the hard part of using EDGAR.

Everything in EDGAR is keyed by CIK, a number nobody knows. A model will be handed
"Apple", "AAPL", "Apple Inc.", or "apple computer" and has to get to 320193. Get
this wrong and every downstream tool call is wrong too, so this module is
deliberately generous about input and explicit about ambiguity.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .client import fetch

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_NOISE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|plc|ltd|limited|holdings?|"
    r"group|the|sa|nv|ag|lp|llc|trust|technologies|technology)\b\.?",
    re.I,
)


def _normalize(name: str) -> str:
    name = name.lower().replace("&", " and ")
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    name = _NOISE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # stripping a noise word can strand a conjunction: "chase & co" -> "chase and"
    return re.sub(r"^(and|the)\s+|\s+(and|the)$", "", name).strip()


_index_cache: list[dict] | None = None


def _index() -> list[dict]:
    """All ~10k SEC registrants with tickers, normalized once."""
    global _index_cache
    if _index_cache is None:
        raw = fetch(TICKERS_URL)
        _index_cache = [
            {
                "cik": str(row["cik_str"]).zfill(10),
                "ticker": row["ticker"].upper(),
                "name": row["title"],
                "_norm": _normalize(row["title"]),
            }
            for row in raw.values()
        ]
    return _index_cache


def search(query: str, limit: int = 5) -> list[dict]:
    """Rank registrants against a name or ticker.

    Exact ticker wins outright. After that it is exact normalized name, then
    prefix, then containment, then fuzzy — each with a score so the caller can
    tell a confident hit from a guess.
    """
    q = query.strip()
    if not q:
        return []

    qn = _normalize(q)
    qu = q.upper()
    # "jp morgan" must reach "jpmorgan chase", so compare despaced too
    qflat = qn.replace(" ", "")
    scored: list[tuple[float, dict]] = []

    for row in _index():
        if row["ticker"] == qu:
            scored.append((1.0, row))
            continue
        if not qn:
            continue
        name = row["_norm"]
        flat = name.replace(" ", "")
        if name == qn or flat == qflat:
            score = 0.95
        elif name.startswith(qn) or qn.startswith(name):
            score = 0.85
        elif flat.startswith(qflat) or qflat.startswith(flat):
            score = 0.84
        elif qn in name:
            score = 0.75
        elif qflat in flat:
            score = 0.74
        else:
            ratio = SequenceMatcher(None, qn, name).ratio()
            if ratio < 0.72:
                continue
            score = ratio * 0.8
        scored.append((score, row))

    scored.sort(key=lambda pair: (-pair[0], len(pair[1]["name"])))
    out = []
    for score, row in scored[:limit]:
        out.append({
            "cik": row["cik"],
            "ticker": row["ticker"],
            "name": row["name"],
            "confidence": round(score, 2),
        })
    return out


def resolve(query: str) -> dict:
    """Return exactly one company, or raise with the alternatives spelled out.

    The error message is written so a model can recover from it in one turn
    instead of guessing again.
    """
    from .client import EdgarError

    hits = search(query, limit=5)
    # Dual-class tickers (BRK-A / BRK-B) are one registrant under one CIK.
    # Treating them as rival candidates invents ambiguity that is not there.
    if hits and len({h["cik"] for h in hits}) == 1:
        best = max(hits, key=lambda h: h["confidence"])
        tickers = sorted({h["ticker"] for h in hits})
        return {**best, "tickers": tickers} if len(tickers) > 1 else best
    if not hits:
        raise EdgarError(
            f"No SEC registrant matched {query!r}. Only companies that file with "
            "the SEC are here, so private companies and most non-US issuers will "
            "not be found. Try the exact legal name or the stock ticker."
        )
    # A weak top hit is worse than no hit: it sends every later call to the
    # wrong company, silently. Make the caller disambiguate instead.
    if hits[0]["confidence"] < 0.7:
        options = "; ".join(f"{h['name']} ({h['ticker']}, CIK {h['cik']})" for h in hits)
        raise EdgarError(
            f"Nothing matched {query!r} with confidence. Retry with a stock ticker "
            f"or the exact legal name. Closest guesses, none trusted: {options}"
        )
    if len(hits) == 1 or hits[0]["confidence"] >= 0.95:
        return hits[0]
    if hits[0]["confidence"] - hits[1]["confidence"] >= 0.2:
        return hits[0]

    options = "; ".join(f"{h['name']} ({h['ticker']}, CIK {h['cik']})" for h in hits)
    raise EdgarError(
        f"{query!r} matched several registrants and none clearly. Call this tool "
        f"again with a ticker or the full legal name. Candidates: {options}"
    )
