# edgar-mcp

An MCP server for SEC EDGAR, built so a model can read **one section of a 10-K
instead of all of it**.

No API key. No account. SEC filings are public record and the data is free.

```
"What does Apple say about supply chain risk?"

  read_filing_section(company="AAPL", accession="0000320193-25-000079",
                      section="risk_factors")

  -> Item 1A — Risk Factors
     17,040 tokens, from a 54,955-token document
```

---

## Why this exists

EDGAR is not hard to call. It is hard to call *well*.

A 10-K runs 300 pages. Handed to a model whole, it costs 50,000+ tokens and
crowds out the reasoning the answer needs. Most EDGAR wrappers mirror the API,
return whole documents, and leave the hard parts — which section, how much of
it, which XBRL tag — to the model.

Those hard parts are the product. This server is six tools, and the design
decisions behind them are the point:

**Six tools, not thirty.** A model needs to find a company, see what it filed,
read part of one filing, and look up a number. Every extra tool is context spent
before the first useful call.

**Accept what a model actually has.** Every tool takes `company` as plain text —
`"Apple"`, `"AAPL"`, or a CIK — and resolves it internally. Forcing a lookup call
before every real call wastes a turn on a problem the server can solve.

**Never return a whole filing.** `read_filing_section` returns one item, capped at
`max_tokens`, with an offset to continue and an explicit note about what was cut.
Silent truncation teaches a model to trust a partial answer.

**Errors are instructions.** Every failure says what to call next:

```
'bank' matched several registrants and none clearly. Call this tool again with
a ticker or the full legal name. Candidates: Bank OZK (OZK, CIK 0001569650);
Bank7 Corp. (BSVN, CIK 0001746129); BANK BRADESCO (BBD, CIK 0001160330)...
```

---

## The two problems worth reading the code for

### Finding the real section, not the table of contents

Every 10-K names each item at least twice: once in the table of contents, once at
the actual section. Match naively and you return forty words of navigation that
look exactly like a successful extraction.

The rule used here: **take the occurrence with the most text before the next item
heading.** A contents entry is followed immediately by the next entry; the real
section is followed by the section. Length separates them, and it holds across
filers who agree on nothing else about formatting.

### "Revenue" is not one tag

Apple reports `RevenueFromContractWithCustomerExcludingAssessedTax`. Older filers
use `Revenues`. Some use `SalesRevenueNet`. A server that knows one tag returns
"no data" for most of the market.

Worse: filers *switch* tags mid-life. NVIDIA's older years sit under one revenue
tag and its recent years under another. Stopping at the first tag that returns
anything silently serves four-year-old numbers that look perfectly current.

So every friendly concept maps to a list of candidate tags, all are fetched, the
newest-reporting one leads, and the others fill in periods it lacks. The response
names the tag that answered, because in finance the definition matters as much as
the number.

---

## Install

```bash
pip install -e .
export EDGAR_MCP_USER_AGENT="Your Name you@example.com"
```

The SEC requires a User-Agent with real contact details and blocks requests
without one. The server refuses to start a request rather than let you get
rate-limited mysteriously.

**Claude Code:**

```bash
claude mcp add edgar -e EDGAR_MCP_USER_AGENT="Your Name you@example.com" -- edgar-mcp
```

**Claude Desktop** — in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "edgar": {
      "command": "edgar-mcp",
      "env": { "EDGAR_MCP_USER_AGENT": "Your Name you@example.com" }
    }
  }
}
```

---

## Tools

| Tool | What it does |
|---|---|
| `find_company` | Name or ticker to CIK, ranked with a confidence score. Refuses rather than guess. |
| `list_filings` | What a company filed, newest first, filterable by form and date. |
| `list_filing_sections` | Which items a filing contains and what each costs to read, in tokens. |
| `read_filing_section` | **The main tool.** One section, budgeted, with an offset to continue. |
| `get_financial_facts` | A reported figure across periods, from XBRL. 14 concepts. |
| `search_filings` | Full-text search across all filings, 2001 to present. |

---

## Evals

Most MCP servers ship with no evidence they work. These produce a number, run
against live SEC data, and gate on a floor.

```bash
python3 evals/run_eval.py
```

```
  company resolution
    accuracy_on_answerable       100.0%    (35/35)
    refusal_rate_on_ambiguous    100.0%    (5/5)

  section retrieval
    grounded                     100.0%
    recall_at_1                   38.5%
    recall_at_3                  100.0%
    cases                           13
```

**The refusal rate matters as much as the accuracy.** A confident wrong CIK sends
every later call to the wrong company and never looks wrong. Five golden cases
are queries that *should* be refused, and refusing them is scored as success.

**`recall_at_1` of 38.5% is reported honestly rather than hidden.** The retrieval
eval ranks sections by naive term overlap, and financial language repeats across
sections — "competition" appears in Business and in Risk Factors. `grounded` at
100% is the number that matters here: the section that should answer a question
does contain the answer. Ranking is a scoring-function problem, not an extraction
problem, and pretending otherwise would be the easy lie.

### What the evals caught

The resolution eval failed on Exxon Mobil, and the cause was not the matcher.
ExxonMobil reorganized: a **holding company** (CIK 2115436) now carries the XOM
ticker while the **operating company** (CIK 34088) still files the 10-K. Both are
defensible answers to the bare name. The eval now accepts either, and
`list_filings` tells the caller which forms each entity actually has.

That class of split exists across the market and I did not know about it before
writing the eval. That is the argument for writing evals.

---

## Tests

```bash
python3 tests/test_protocol.py
```

Nine checks over the **real MCP stdio protocol**, not direct function calls —
launching the server as a subprocess and talking to it the way a client does.
Direct calls skip serialization, schema validation, and error translation, which
are the three places an MCP server actually breaks.

One of those checks exists because of a bug found this way: the SDK treats an
unrecognized exception as a crash and replaces the message with
`Error executing tool <name>`. Every recovery instruction written into the error
text was being thrown away. The fix is to raise `ToolError`, whose message is
delivered intact — invisible from a direct call, obvious over the protocol.

---

## Limitations

- **Full-text search starts at 2001.** Older filings are listed but not searchable.
- **XBRL means US-GAAP.** Foreign private issuers filing under IFRS often return
  no facts. The error says so instead of returning an empty list.
- **Part III is frequently incorporated by reference** to the proxy, so Items
  10–14 are often one sentence pointing at a DEF 14A. `list_filing_sections`
  shows the real size so this is visible before you read.
- **Section extraction is heuristic.** It handles the common 10-K layouts. Exotic
  formatting, and some older scanned filings, will not parse cleanly.
- Responses are cached to `~/.cache/edgar-mcp` for 24 hours. Set
  `EDGAR_MCP_CACHE` to move it.

---

## Not investment advice

This reads public filings. It does not interpret them, and nothing it returns is
a recommendation.

## License

MIT
