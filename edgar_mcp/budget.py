"""Keeping a tool response inside a context budget, and saying so when it doesn't.

A tool that silently truncates teaches a model to trust a partial answer. Every
truncation here is announced, measured, and paired with the call that gets the
rest.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 4          # close enough for budgeting; never for billing
DEFAULT_MAX_TOKENS = 6000


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def clip(text: str, max_tokens: int = DEFAULT_MAX_TOKENS, *,
         offset: int = 0, more_hint: str = "") -> dict:
    """Return a window of text plus an honest description of what was cut."""
    budget = max(200, max_tokens) * CHARS_PER_TOKEN
    total = len(text)
    start = max(0, min(offset, total))
    window = text[start:start + budget]

    # Prefer to end on a paragraph so a sentence is never cut mid-clause.
    if start + budget < total:
        cut = window.rfind("\n\n")
        if cut > budget * 0.6:
            window = window[:cut]

    end = start + len(window)
    out = {
        "text": window,
        "returned_tokens": estimate_tokens(window),
        "total_tokens": estimate_tokens(text),
        "offset": start,
        "next_offset": end if end < total else None,
        "complete": start == 0 and end >= total,
    }
    if not out["complete"]:
        pct = (end - start) / total * 100 if total else 100
        out["truncation_notice"] = (
            f"Showing characters {start:,}-{end:,} of {total:,} ({pct:.0f}% of the "
            f"section)." + (f" {more_hint}" if more_hint else "")
        )
        if out["next_offset"] is not None and more_hint:
            out["truncation_notice"] += f" Pass offset={out['next_offset']} for the next window."
    return out
