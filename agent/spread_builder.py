"""Vertical credit spread construction.

Takes the short-put candidates and pairs each with a long put further out of
the money, producing a bull put spread: sell the nearer strike, buy the further
one. The long leg is what makes the position defined-risk — it caps the loss at
the width of the spread, so no gap down can produce an unbounded loss the way a
naked short put would.

All the arithmetic here is quoted-price arithmetic, deliberately conservative:
the credit assumes selling at the *bid* and buying at the *ask*, which is the
worst realistic fill rather than the mid-price fantasy. A spread that only looks
good at the mid does not survive this function.
"""

#
# PORTED from the previous build (~/alpaca-agent) without logic changes.
# Only the data source changed: Alpaca is now reached through the MCP server,
# and agent/adapters.py normalises its JSON into the shapes below. The
# LangGraph node wrapper was removed - this build's orchestrator is a plain
# async loop, not a graph - but every pure function is byte-for-byte the
# logic that passed 271 tests, including the corrected constants in
# PLAN.md section 3.


from __future__ import annotations

import logging
from typing import Any

from .config import AgentConfig

logger = logging.getLogger(__name__)


def build_spreads(
    candidates: list[dict[str, Any]],
    chain_index: dict[str, dict[str, Any]],
    account_balance: float,
    config: AgentConfig,
) -> list[dict[str, Any]]:
    """Construct and rank bull put spreads from short-leg candidates.

    For each candidate short strike, tries every configured width and computes:

    * ``net_credit``  = (short bid - long ask) x 100, the cash received
    * ``max_loss``    = width x 100 - net_credit, the capped downside
    * ``prob_profit`` = 100 - |short delta| x 100, the rough odds of expiring worthless
    * ``max_contracts`` = floor(2% of NAV / max_loss), the position size cap

    Spreads whose per-contract max loss already exceeds the 2%-of-NAV budget are
    dropped here rather than passed on to be rejected — the risk gate stays the
    single authority, but there is no reason to hand it work that cannot pass.

    Ranking multiplies return-on-risk by probability of profit. A spread paying
    10% of its max loss with an 85% chance of expiring worthless outranks one
    paying 20% with a 60% chance; the first is the premium-selling trade, the
    second is a directional bet wearing its clothes.
    """
    min_width, max_width = config.spread_widths
    max_loss_budget = account_balance * config.max_loss_pct
    top_n = int(config.spread_builder.get("top_n", 10))

    spreads: list[dict[str, Any]] = []
    skipped = {"no_long_leg": 0, "no_credit": 0, "over_budget": 0}

    for short in candidates:
        expiry = short["expiry"]
        short_strike = short["strike"]

        for width in _widths(min_width, max_width):
            long_strike = short_strike - width
            long_leg = chain_index.get(f"{expiry}|{float(long_strike)}")
            if long_leg is None or long_leg["ask"] <= 0:
                skipped["no_long_leg"] += 1
                continue

            # Conservative fill assumption: hit the bid on the short, lift the
            # ask on the long.
            net_credit = round((short["bid"] - long_leg["ask"]) * 100.0, 2)
            if net_credit <= 0:
                skipped["no_credit"] += 1
                continue

            max_loss = round(width * 100.0 - net_credit, 2)
            if max_loss <= 0:
                # A credit exceeding the width would be an arbitrage, which in
                # practice means a stale or crossed quote. Never trade it.
                skipped["no_credit"] += 1
                continue

            if max_loss > max_loss_budget:
                skipped["over_budget"] += 1
                continue

            max_contracts = int(max_loss_budget // max_loss)
            if max_contracts < 1:
                skipped["over_budget"] += 1
                continue

            prob_profit = round(100.0 - abs(short["delta"]) * 100.0, 2)
            return_on_risk = round(net_credit / max_loss * 100.0, 2)
            long_delta = long_leg.get("delta") or 0.0
            # Position delta, not contract delta. We are SHORT the near strike
            # and LONG the far one, so the short leg's delta enters with a
            # flipped sign. A bull put spread is net LONG delta (bullish) —
            # subtracting in the other order would label it bearish and feed the
            # portfolio-delta rule a number with the wrong sign.
            net_delta = round(long_delta - short["delta"], 4)

            spreads.append(
                {
                    "type": "bull_put",
                    "ticker": short["ticker"],
                    "expiry": expiry,
                    "dte": short["dte"],
                    "sell_symbol": short["symbol"],
                    "buy_symbol": long_leg["symbol"],
                    "sell_strike": short_strike,
                    "buy_strike": float(long_strike),
                    "spread_width": float(width),
                    "sell_bid": short["bid"],
                    "sell_ask": short["ask"],
                    "buy_bid": long_leg["bid"],
                    "buy_ask": long_leg["ask"],
                    "sell_delta": short["delta"],
                    "buy_delta": round(long_delta, 4),
                    "net_delta": net_delta,
                    "sell_theta": short.get("theta"),
                    "sell_vega": short.get("vega"),
                    "sell_gamma": short.get("gamma"),
                    "sell_iv": short.get("iv"),
                    "net_credit": net_credit,
                    "mid_credit": round((short["mid"] - (long_leg["bid"] + long_leg["ask"]) / 2) * 100.0, 2),
                    "max_loss": max_loss,
                    "return_on_risk_pct": return_on_risk,
                    "prob_profit": prob_profit,
                    "max_contracts": max_contracts,
                    "breakeven": round(short_strike - net_credit / 100.0, 2),
                    "score": round(return_on_risk * prob_profit / 100.0, 3),
                }
            )

    spreads.sort(key=lambda s: s["score"], reverse=True)
    for rank, spread in enumerate(spreads, start=1):
        spread["rating"] = _rating(rank, len(spreads))

    logger.info(
        "Built %d spreads from %d candidates (skipped: %s)",
        len(spreads), len(candidates), skipped,
    )
    return spreads[:top_n]


def _widths(min_width: float, max_width: float) -> list[float]:
    """Whole-dollar spread widths in the configured range.

    Index ETF options list in $1 strike increments, so every integer width
    between the bounds is constructible.
    """
    return [float(w) for w in range(int(min_width), int(max_width) + 1)]


def _rating(rank: int, total: int) -> str:
    """A/B/C label by rank, purely for the dashboard and journal."""
    if total == 0:
        return "C"
    if rank <= max(1, total // 4):
        return "A"
    if rank <= max(2, total // 2):
        return "B"
    return "C"
