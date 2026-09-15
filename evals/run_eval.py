#!/usr/bin/env python3
"""Two evals, both deterministic, both run against live SEC data.

Most MCP servers ship with no evidence they work. These produce a number.

1. RESOLUTION — does a company name reach the right CIK? Scored on accuracy,
   and critically on the refusal cases: a genuinely ambiguous query must be
   refused, because a confident wrong CIK sends every later call to the wrong
   company without ever looking wrong.

2. SECTION RETRIEVAL — given a real question, does the section that should
   answer it actually contain the answer terms, and does it rank first? This
   tests the part that is easy to get subtly wrong: a table-of-contents match
   returns forty words and looks like a successful extraction.

    python3 evals/run_eval.py
    python3 evals/run_eval.py --only resolution
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edgar_mcp import companies, filings, sections   # noqa: E402
from edgar_mcp.client import EdgarError              # noqa: E402

HERE = Path(__file__).parent


def eval_resolution() -> dict:
    spec = json.loads((HERE / "golden_companies.json").read_text())
    resolved = refused = 0
    should_resolve = should_refuse = 0
    failures = []

    for case in spec["cases"]:
        want = case["cik"]
        try:
            got = companies.resolve(case["query"])["cik"]
        except EdgarError:
            got = None

        accepted = want if isinstance(want, list) else [want]
        if want is None:
            should_refuse += 1
            if got is None:
                refused += 1
            else:
                failures.append(f"{case['query']!r} should have been refused, got {got}")
        else:
            should_resolve += 1
            if got in accepted:
                resolved += 1
            else:
                failures.append(f"{case['query']!r} -> {got} (wanted {want})")

    return {
        "eval": "company resolution",
        "accuracy_on_answerable": resolved / should_resolve,
        "refusal_rate_on_ambiguous": refused / should_refuse,
        "answerable": f"{resolved}/{should_resolve}",
        "ambiguous": f"{refused}/{should_refuse}",
        "failures": failures,
    }


def _score(text: str, terms: list[str]) -> float:
    low = text.lower()
    return sum(1 for t in terms if t.lower() in low) / len(terms)


def eval_section_retrieval() -> dict:
    spec = json.loads((HERE / "golden_questions.json").read_text())
    texts: dict[str, str] = {}
    for f in spec["filings"]:
        cik = companies.resolve(f["company"])["cik"]
        meta = filings.find_filing(cik, f["accession"])
        doc = sections.load_document_text(meta["url"])
        for s in sections.list_sections(doc):
            texts[s["section"]] = sections.extract_section(doc, s["section"])[1]

    at1 = at3 = grounded = 0
    failures = []
    for case in spec["cases"]:
        want = case["section"]
        if want not in texts:
            failures.append(f"{want} was not extracted from the filing at all")
            continue

        # Are the answer terms actually present in the section we claim answers it?
        if _score(texts[want], case["terms"]) >= 0.5:
            grounded += 1
        else:
            failures.append(f"{want}: only "
                            f"{_score(texts[want], case['terms']):.0%} of terms present "
                            f"for {case['question']!r}")

        ranked = sorted(texts, key=lambda k: -_score(texts[k], case["terms"]))
        if ranked[0] == want:
            at1 += 1
        if want in ranked[:3]:
            at3 += 1

    n = len(spec["cases"])
    return {
        "eval": "section retrieval",
        "grounded": grounded / n,
        "recall_at_1": at1 / n,
        "recall_at_3": at3 / n,
        "cases": n,
        "failures": failures,
    }


def show(r: dict) -> None:
    print(f"\n  {r['eval']}")
    for k, v in r.items():
        if k in ("eval", "failures"):
            continue
        print(f"    {k:<28} {v:.1%}" if isinstance(v, float) else f"    {k:<28} {v}")
    for f in r["failures"][:8]:
        print(f"      - {f}")
    if len(r["failures"]) > 8:
        print(f"      ... and {len(r['failures']) - 8} more")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["resolution", "retrieval"])
    args = ap.parse_args()

    results = []
    if args.only in (None, "resolution"):
        results.append(eval_resolution())
    if args.only in (None, "retrieval"):
        results.append(eval_section_retrieval())
    for r in results:
        show(r)

    floors = {"company resolution": ("accuracy_on_answerable", 0.90),
              "section retrieval": ("grounded", 0.85)}
    bad = [r["eval"] for r in results
           if r[floors[r["eval"]][0]] < floors[r["eval"]][1]]
    print(f"\n  {'FAILED: ' + ', '.join(bad) if bad else 'all evals above their floor'}\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
