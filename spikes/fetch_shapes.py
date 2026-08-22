"""Stage 1 gate: fetch the market clock, the account, and one SPY option chain with Greeks.

Captures the raw JSON shape of each result to spikes/shapes_raw.json. agent/adapters.py is
written against these captured shapes, not against guesses.
"""
import asyncio, json, os, sys, datetime as dt
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

PARAMS = StdioServerParameters(
    command="uvx",
    args=["alpaca-mcp-server"],
    env={
        "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
        "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
        "ALPACA_PAPER_TRADE": os.getenv("ALPACA_PAPER_TRADE", "true"),
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
    },
)

def unwrap(result):
    """Return (parsed_structured, raw_text_blocks) for an MCP CallToolResult."""
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    return {
        "is_error": result.is_error,
        "structured_content": result.structured_content,
        "content_types": [getattr(c, "type", None) for c in result.content],
        "text_blocks": texts,
    }

async def main() -> None:
    captured = {}
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. market clock
            captured["get_clock"] = unwrap(await session.call_tool("get_clock", {}))

            # 2. account
            captured["get_account_info"] = unwrap(await session.call_tool("get_account_info", {}))

            # 3. SPY option chain with greeks - narrowed to a put window ~7-14 DTE
            today = dt.date.today()
            args = {
                "underlying_symbol": "SPY",
                "type": "put",
                "expiration_date_gte": (today + dt.timedelta(days=7)).isoformat(),
                "expiration_date_lte": (today + dt.timedelta(days=14)).isoformat(),
                "limit": 20,
            }
            captured["get_option_chain"] = unwrap(await session.call_tool("get_option_chain", args))
            captured["get_option_chain"]["_args"] = args

    out = os.path.join(os.path.dirname(__file__), "shapes_raw.json")
    with open(out, "w") as f:
        json.dump(captured, f, indent=2, default=str)

    for name, cap in captured.items():
        print("=" * 72)
        print(f"{name}  isError={cap['is_error']}  content_types={cap['content_types']}")
        print(f"structuredContent present: {cap['structured_content'] is not None}")
        for t in cap["text_blocks"]:
            print(t[:1800])
        print()

asyncio.run(main())
