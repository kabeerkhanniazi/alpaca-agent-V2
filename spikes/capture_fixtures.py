"""Capture real MCP responses as test fixtures.

Uses agent.adapters.chain_request to build the call, so the fixture and the
production request path cannot drift apart.
"""
import asyncio, json, sys
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from agent.mcp_client import MCPClient
from agent.adapters import adapt_spot, chain_request
from agent.config import load_config

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

async def main():
    config = load_config(with_credentials=False)
    async with MCPClient() as mcp:
        clock = await mcp.call_read("get_clock", {})
        account = await mcp.call_read("get_account_info", {})
        spot_raw = await mcp.call_read("get_stock_latest_trade", {"symbols": "SPY", "feed": "iex"})
        spot = adapt_spot(spot_raw)

        args = chain_request(
            "SPY", spot,
            feed=config.options_feed,
            bracket_pct=config.chain["strike_bracket_pct"],
            min_dte=config.min_dte, max_dte=config.max_dte,
            limit=config.chain["limit"],
        )
        chain = await mcp.call_read("get_option_chain", args)
        positions = await mcp.call_read("get_all_positions", {})
        bars = await mcp.call_read("get_stock_bars", {
            "symbols": "SPY", "timeframe": "1Day", "feed": "iex",
            "start": "2025-08-22", "limit": 400})

    payload = {
        "_captured": date.today().isoformat(),
        "_note": "Real responses from the live Alpaca MCP server. The chain request "
                 "was built by agent.adapters.chain_request so fixture and production "
                 "path cannot drift.",
        "spot": spot,
        "chain_request_args": args,
        "get_clock": clock,
        "get_account_info": account,
        "get_stock_latest_trade": spot_raw,
        "get_option_chain": chain,
        "get_all_positions": positions,
        "get_stock_bars": bars,
    }
    (OUT / "mcp_responses.json").write_text(json.dumps(payload, indent=1))

    snaps = chain.get("snapshots", {})
    withg = {k: v for k, v in snaps.items() if "greeks" in v}
    deltas = sorted(v["greeks"]["delta"] for v in withg.values())
    intgt = [d for d in deltas if -0.20 <= d <= -0.15]
    print(f"spot {spot}  bracket {args['strike_price_gte']}..{args['strike_price_lte']}")
    print(f"contracts {len(snaps)}, with greeks {len(withg)}")
    print(f"delta range {deltas[0]:.4f}..{deltas[-1]:.4f}")
    print(f"in target window -0.20..-0.15: {len(intgt)}")
    print(f"bars: {len((bars or {}).get('bars') or [])}")

asyncio.run(main())
