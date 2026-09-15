"""End-to-end test over the real MCP stdio protocol, not direct function calls.

Direct calls skip serialization, schema validation, and error translation — the
three places an MCP server actually breaks. This launches the server as a
subprocess and talks to it the way a client does.
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

APPLE_10K = "0000320193-25-000079"


def result_text(res):
    return "\n".join(c.text for c in res.content if hasattr(c, "text"))


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "edgar_mcp.server"],
        env={**os.environ},
    )
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        passed, failed = passed + ok, failed + (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            check("handshake", info.server_info.name == "edgar",
                  f"server={info.server_info.name}")

            tools = (await session.list_tools()).tools
            check("six tools advertised", len(tools) == 6, f"got {len(tools)}")
            check("every tool documented",
                  all(t.description and len(t.description) > 120 for t in tools))

            r = await session.call_tool("find_company", {"query": "AAPL"})
            check("find_company resolves a ticker",
                  "0000320193" in result_text(r))

            r = await session.call_tool("get_financial_facts",
                                        {"company": "Microsoft", "concept": "revenue",
                                         "periods": 2})
            check("facts accept a plain name, no CIK lookup first",
                  "MICROSOFT" in result_text(r).upper())

            r = await session.call_tool(
                "read_filing_section",
                {"company": "AAPL", "accession": APPLE_10K,
                 "section": "risk_factors", "max_tokens": 500})
            body = result_text(r)
            check("section read is budgeted, not the whole filing",
                  "truncation_notice" in body and "next_offset" in body)

            # the point of the whole error design: the message must survive
            r = await session.call_tool("get_financial_facts",
                                        {"company": "bank", "concept": "revenue"})
            msg = result_text(r)
            # The SDK prefixes the message with "Error executing tool <name>:".
            # What matters is that our recovery text survives after the prefix.
            check("ambiguous company returns the candidates to choose from",
                  r.is_error and "Bank OZK" in msg and "full legal name" in msg)

            r = await session.call_tool("get_financial_facts",
                                        {"company": "AAPL", "concept": "ebitda"})
            msg = result_text(r)
            check("unknown concept lists the valid ones",
                  r.is_error and "revenue" in msg and "net_income" in msg)

            r = await session.call_tool("read_filing_section",
                                        {"company": "AAPL", "section": "mdna",
                                         "accession": "9999999999-99-999999"})
            check("bad accession points at list_filings",
                  r.is_error and "list_filings" in result_text(r))

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
