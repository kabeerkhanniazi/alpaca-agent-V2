"""Stage 4 gate: the ported deterministic core, driven by live MCP data.

Fetches through the MCP read tools, normalises with agent.adapters, and runs the
ported pipeline — candidates, spreads, nine-rule verdict — with no LLM anywhere
in it. If this produces a verdict, the port is sound and Stage 5 only has to add
the model.

    python spikes/gate_stage4.py [TICKER]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from agent.adapters import (  # noqa: E402
    adapt_account, adapt_bars, adapt_chain, adapt_clock, adapt_positions,
    adapt_spot, chain_request, diagnose_bracket, option_positions,
)
from agent.config import load_config  # noqa: E402
from agent.iv import atm_implied_volatility, compute_iv_rank  # noqa: E402
from agent.mcp_client import MCPClient  # noqa: E402
from agent.options_calculator import calculate_options_opportunities, index_chain  # noqa: E402
from agent.position_manager import decide_exits, group_into_spreads, parse_position  # noqa: E402
from agent.risk_gate import risk_gate_check  # noqa: E402
from agent.spread_builder import build_spreads  # noqa: E402


async def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    config = load_config(with_credentials=False)

    async with MCPClient() as mcp:
        clock = adapt_clock(await mcp.call_read("get_clock", {}))
        account = adapt_account(await mcp.call_read("get_account_info", {}))
        positions = adapt_positions(await mcp.call_read("get_all_positions", {}))
        spot = adapt_spot(await mcp.call_read(
            "get_stock_latest_trade", {"symbols": ticker, "feed": "iex"}))

        args = chain_request(
            ticker, spot,
            feed=config.options_feed,
            bracket_pct=config.chain["strike_bracket_pct"],
            min_dte=config.min_dte, max_dte=config.max_dte,
            limit=config.chain["limit"],
        )
        chain = adapt_chain(await mcp.call_read("get_option_chain", args))
        # An explicit start is required: without it the server returns only a
        # handful of recent bars, and IV rank's realized-vol proxy needs a year.
        start = (date.today() - timedelta(days=400)).isoformat()
        bars = adapt_bars(await mcp.call_read("get_stock_bars", {
            "symbols": ticker, "timeframe": "1Day", "feed": "iex",
            "start": start, "limit": 10000}))

    print("=" * 74)
    print(f"LIVE MCP DATA — {ticker}")
    print("=" * 74)
    print(f"  market open:   {clock.is_open}")
    print(f"  NAV:           ${account.equity:,.2f}   buying power ${account.buying_power:,.2f}")
    print(f"  spot:          {spot}")
    print(f"  open positions:{len(positions)} ({len(option_positions(positions))} option legs)")
    print(f"  chain:         {len(chain)} contracts, "
          f"bracket {args['strike_price_gte']}..{args['strike_price_lte']}")
    print(f"  bars:          {len(bars)} daily")

    atm_iv = atm_implied_volatility(chain, spot)
    iv = compute_iv_rank(
        ticker=ticker, atm_iv=atm_iv, closes=list(bars["close"]),
        history_path=config.paths["iv_history"], iv_config=config.iv,
    )
    print(f"  ATM IV:        {atm_iv}")
    print(f"  IV rank:       {iv.get('iv_rank')} (source: {iv.get('iv_rank_source')})")

    print()
    print("=" * 74)
    print("PORTED PIPELINE")
    print("=" * 74)

    candidates = calculate_options_opportunities(chain, ticker, spot, config)
    print(f"  candidates in the delta window: {len(candidates)}")

    if not candidates:
        diagnosis = diagnose_bracket(
            chain, config.delta_range,
            (args["strike_price_gte"], args["strike_price_lte"]),
            args["limit"], 0,
        )
        print(f"  diagnosis: {diagnosis.outcome}")
        print(f"    {diagnosis.detail}")
        return 1 if diagnosis.is_defect else 0

    spreads = build_spreads(candidates, index_chain(chain), account.equity, config)
    print(f"  spreads built: {len(spreads)}")
    if not spreads:
        print("  no spread cleared the per-spread loss budget")
        return 1

    top = spreads[0]
    print(f"  best spread:   {ticker} {top['expiry']} "
          f"{top['sell_strike']:g}/{top['buy_strike']:g}")
    print(f"                 credit ${top['net_credit']:.2f}, "
          f"max loss ${top['max_loss']:.2f}, "
          f"short delta {top['sell_delta']:.4f}, net delta {top['net_delta']:+.4f}")

    parsed = [parse_position(p) for p in option_positions(positions)]
    portfolio = {
        "nav": account.equity,
        "buying_power": account.buying_power,
        "starting_buying_power": account.buying_power,
        "daily_pnl_pct": 0.0,
        "net_delta_dollars": 0.0,
        "open_positions": parsed,
    }

    exits = decide_exits(portfolio, config)
    print(f"  open spreads:  {len(group_into_spreads(parsed))}, exits due: {len(exits)}")

    print()
    print("=" * 74)
    print("NINE-RULE RISK GATE")
    print("=" * 74)
    verdict = risk_gate_check(top, portfolio, config)
    for check in verdict.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.rule:<24} {check.name}")
        print(f"         observed {check.observed}   limit {check.limit}")

    print()
    print(f"  VERDICT: {'APPROVED' if verdict.approved else 'REJECTED'} "
          f"— {verdict.contracts} contract(s)")
    print(f"  {verdict.reason}")

    print()
    print("=" * 74)
    print(f"Stage 4 gate: PASS — live MCP JSON drove the ported core to a "
          f"{len(verdict.checks)}-rule verdict, no LLM involved")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
