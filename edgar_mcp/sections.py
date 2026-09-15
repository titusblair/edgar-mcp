"""Pulling one named section out of a filing, instead of the whole document.

This is the part that makes an EDGAR server usable by a model at all. A 10-K runs
300 pages and several hundred thousand tokens. A question like "what does Apple
say about supply chain risk" needs Item 1A and nothing else.

The hard bit is not finding the words "Item 1A" — it is finding the *right*
occurrence. Every 10-K names each item at least twice: once in the table of
contents and once at the real section. Naive matching lands you in the TOC and
returns forty words of navigation.

The rule used here: an item heading may appear many times, so take the occurrence
with the most text before the next item heading. A table of contents entry is
followed immediately by the next entry; the real section is followed by the
section. Length is the signal that separates them, and it holds across filers who
agree on nothing else about formatting.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .client import EdgarError, fetch

# The items worth asking for by name, mapped to the regex that finds the heading.
SECTIONS = {
    "business":            (r"item\s*1\b(?!\s*[ab])", "Item 1 — Business"),
    "risk_factors":        (r"item\s*1a\b",           "Item 1A — Risk Factors"),
    "unresolved_comments": (r"item\s*1b\b",           "Item 1B — Unresolved Staff Comments"),
    "properties":          (r"item\s*2\b",            "Item 2 — Properties"),
    "legal_proceedings":   (r"item\s*3\b",            "Item 3 — Legal Proceedings"),
    "market_for_stock":    (r"item\s*5\b",            "Item 5 — Market for Registrant's Common Equity"),
    "mdna":                (r"item\s*7\b(?!\s*a)",    "Item 7 — Management's Discussion and Analysis"),
    "market_risk":         (r"item\s*7a\b",           "Item 7A — Quantitative and Qualitative Disclosures About Market Risk"),
    "financial_statements":(r"item\s*8\b",            "Item 8 — Financial Statements and Supplementary Data"),
    "controls":            (r"item\s*9a\b",           "Item 9A — Controls and Procedures"),
    "directors":           (r"item\s*10\b",           "Item 10 — Directors and Executive Officers"),
    "executive_comp":      (r"item\s*11\b",           "Item 11 — Executive Compensation"),
}

# Order matters: a section ends where the next one starts.
_ORDER = ["business", "risk_factors", "unresolved_comments", "properties",
          "legal_proceedings", "market_for_stock", "mdna", "market_risk",
          "financial_statements", "controls", "directors", "executive_comp"]

# Items nobody asks for by name, kept only so the last requested section has
# something to end at. Without these, Item 11 runs to the end of the file.
_TERMINATORS = [
    r"item\s*12\b", r"item\s*13\b", r"item\s*14\b", r"item\s*15\b",
    r"item\s*16\b", r"signatures?\b", r"exhibit\s+index\b",
]

_BLOCK_TAGS = {"p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4", "h5", "table"}


class _Text(HTMLParser):
    """Tags to text, keeping block boundaries so headings stay on their own line."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = raw.replace("\xa0", " ")
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html)
    except Exception:  # malformed filings are common; keep whatever parsed
        pass
    return parser.text()


def _heading_positions(text: str, pattern: str) -> list[int]:
    """Offsets where an item heading plausibly starts.

    Anchored to a line start so "as described in Item 1A above" is not a hit.
    """
    rx = re.compile(rf"^\s*{pattern}[\s.:\-—]*", re.I | re.M)
    return [m.start() for m in rx.finditer(text)]


def extract_section(text: str, key: str) -> tuple[str, str]:
    """Return (heading label, section body). Raises if the item is not present."""
    if key not in SECTIONS:
        raise EdgarError(
            f"Unknown section {key!r}. Valid sections: {', '.join(SECTIONS)}."
        )
    pattern, label = SECTIONS[key]
    starts = _heading_positions(text, pattern)
    if not starts:
        raise EdgarError(
            f"{label} does not appear in this document. Not every form has every "
            "item — a 10-Q has no Item 1A in the 10-K sense, and smaller reporting "
            "companies may omit it. Try list_sections on this filing first."
        )

    # Where does each candidate end? At the next item heading of any kind.
    later = _ORDER[_ORDER.index(key) + 1:]
    boundaries = [SECTIONS[k][0] for k in later] + _TERMINATORS
    next_starts = sorted(
        pos for pat in boundaries for pos in _heading_positions(text, pat)
    )

    best, best_len = None, -1
    for start in starts:
        end = next((p for p in next_starts if p > start), len(text))
        # A table-of-contents hit is followed immediately by the next entry.
        # The real section is followed by the section itself.
        if end - start > best_len:
            best, best_len = (start, end), end - start

    start, end = best
    body = text[start:end].strip()
    if len(body) < 200:
        raise EdgarError(
            f"{label} was found but holds only {len(body)} characters, which "
            "usually means the filing cross-references an exhibit instead of "
            "stating the item inline. Read the filing index for exhibits."
        )
    return label, body


def list_sections(text: str) -> list[dict]:
    """Which items this document actually contains, and how big each one is."""
    found = []
    for key in _ORDER:
        try:
            label, body = extract_section(text, key)
        except EdgarError:
            continue
        found.append({
            "section": key,
            "heading": label,
            "characters": len(body),
            "approx_tokens": len(body) // 4,
        })
    return found


def load_document_text(url: str) -> str:
    html = fetch(url, as_json=False)
    return html_to_text(html)
