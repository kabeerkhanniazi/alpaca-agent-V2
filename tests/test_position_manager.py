"""Spread-level exit management — PLAN.md section 3.2.

Ported alongside the fix it protects. In the previous build these assertions
lived in the LangGraph suite; this build has no graph, so they are rewritten
against the pure functions `group_into_spreads` and `decide_exits`. The
scenarios and expected numbers are unchanged.

**The bug this exists to prevent.** Alpaca reports each leg of a multi-leg
position as its own row. A short put decaying into profit while its long wing
decays into loss will, judged per leg, trip the profit target on the short side
alone — closing it and leaving a naked long put behind. Every exit decision must
therefore be made on the whole spread.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent.adapters import adapt_positions
from agent.position_manager import decide_exits, group_into_spreads, parse_position

from conftest import occ_symbol


def leg(strike: float, qty: int, cost_basis: float, unrealized_pl: float, days: int = 9):
    """One leg of an open spread, as MCP's `get_all_positions` returns it.

    Deliberately raw JSON rather than a hand-built object: routing it through
    `adapt_positions` means these tests exercise the same adapter the live cycle
    uses, so a shape change breaks them here rather than in production.
    """
    market_value = cost_basis + unrealized_pl
    return {
        "symbol": occ_symbol("SPY", date.today() + timedelta(days=days), strike),
        "asset_class": "us_option",
        "qty": qty,
        "avg_entry_price": abs(cost_basis) / 100,
        "cost_basis": cost_basis,
        "unrealized_pl": unrealized_pl,
        "unrealized_plpc": unrealized_pl / abs(cost_basis) if cost_basis else 0,
        "market_value": market_value,
        "current_price": abs(market_value) / 100,
    }


def parsed(*legs) -> list:
    return [parse_position(p) for p in adapt_positions(list(legs))]


def portfolio_of(*legs) -> dict:
    return {"open_positions": parsed(*legs)}


# --------------------------------------------------------------- grouping


def test_two_legs_of_one_spread_group_together():
    groups = group_into_spreads(parsed(
        leg(753.0, -4, -684.0, 420.0),
        leg(748.0, 4, 436.0, -160.0),
    ))
    assert len(groups) == 1
    assert groups[0]["leg_count"] == 2


def test_spreads_on_different_expiries_are_separate_positions():
    """Grouping is by (underlying, expiry); two expiries are two spreads."""
    groups = group_into_spreads(parsed(
        leg(753.0, -4, -684.0, 420.0, days=9),
        leg(748.0, 4, 436.0, -160.0, days=9),
        leg(750.0, -4, -600.0, 100.0, days=12),
        leg(745.0, 4, 400.0, -40.0, days=12),
    ))
    assert len(groups) == 2
    assert all(group["leg_count"] == 2 for group in groups)


def test_a_group_sums_the_pnl_of_its_legs():
    group = group_into_spreads(parsed(
        leg(753.0, -4, -684.0, 420.0),
        leg(748.0, 4, 436.0, -160.0),
    ))[0]
    assert group["unrealized_pl"] == pytest.approx(260.0)
    assert group["cost_basis"] == pytest.approx(-248.0)


# ------------------------------------------------------- ★ the orphan trap


def test_a_profitable_spread_exits_as_one_not_two(config):
    """The orphan-leg trap.

    The short leg alone is up $420 on a $684 basis — 61%, past the 50% target.
    Judged per leg it would be closed on its own, orphaning the long put. As a
    spread the two legs are one decision.
    """
    exits = decide_exits(
        portfolio_of(
            leg(753.0, qty=-4, cost_basis=-684.0, unrealized_pl=420.0),
            leg(748.0, qty=4, cost_basis=436.0, unrealized_pl=-160.0),
        ),
        config,
    )
    assert len(exits) == 1, "the two legs are one spread, not two exits"
    assert exits[0]["leg_count"] == 2
    assert len(exits[0]["symbols"]) == 2, "both legs must be closed together"


def test_exit_pnl_is_measured_on_the_whole_spread(config):
    """Net P&L is +$260, not the short leg's +$420."""
    exits = decide_exits(
        portfolio_of(
            leg(753.0, qty=-4, cost_basis=-684.0, unrealized_pl=420.0),
            leg(748.0, qty=4, cost_basis=436.0, unrealized_pl=-160.0),
        ),
        config,
    )
    assert exits[0]["unrealized_pl"] == pytest.approx(260.0, abs=0.01)


def test_a_spread_below_the_profit_target_is_left_alone(config):
    """Net capture here is 10% of the credit — nowhere near the 50% target.

    Per-leg, the short is up 24% and the long down; neither reading should
    produce an exit, and the spread reading is the only correct one.
    """
    assert decide_exits(
        portfolio_of(
            leg(753.0, qty=-4, cost_basis=-248.0, unrealized_pl=60.0),
            leg(748.0, qty=4, cost_basis=0.0, unrealized_pl=-35.0),
        ),
        config,
    ) == []


# --------------------------------------------------------- the three triggers


def test_a_losing_spread_is_stopped_out(config):
    """Down more than the credit received: cut before it reaches max loss."""
    exits = decide_exits(
        portfolio_of(
            leg(753.0, qty=-4, cost_basis=-248.0, unrealized_pl=-400.0),
            leg(748.0, qty=4, cost_basis=0.0, unrealized_pl=100.0),
        ),
        config,
    )
    assert len(exits) == 1
    assert "stop" in exits[0]["exit_reason"].lower()


def test_a_spread_at_the_dte_floor_closes_regardless_of_pnl(config):
    """Gamma and pin risk in the final days are not paid for by the theta left."""
    exits = decide_exits(
        portfolio_of(
            leg(753.0, qty=-4, cost_basis=-248.0, unrealized_pl=5.0, days=1),
            leg(748.0, qty=4, cost_basis=0.0, unrealized_pl=-2.0, days=1),
        ),
        config,
    )
    assert len(exits) == 1
    assert "DTE" in exits[0]["exit_reason"]


def test_no_positions_means_no_exits(config):
    assert decide_exits({"open_positions": []}, config) == []


def test_a_zero_cost_basis_spread_is_skipped(config):
    """With no credit recorded there is no denominator to judge capture against."""
    assert decide_exits(
        portfolio_of(
            leg(753.0, qty=-4, cost_basis=0.0, unrealized_pl=100.0),
            leg(748.0, qty=4, cost_basis=0.0, unrealized_pl=-20.0),
        ),
        config,
    ) == []


def test_a_positive_cost_basis_is_still_evaluated_via_its_magnitude(config):
    """Pins current behaviour, which is safe only because of what the agent trades.

    `decide_exits` judges capture against ``abs(cost_basis)``. For the credit
    spreads this agent opens the combined basis is always negative, so the
    magnitude is the credit and the maths is right. A *debit* spread would take
    the same path and produce an inverted reading — but nothing in this system
    can open one: `agent.tools.parse_proposal` rejects an inverted spread and
    `agent.adapters.order_request` refuses a non-negative limit price.

    Asserted rather than fixed: the ported logic is under a no-rewrite rule, and
    the condition that would make it wrong is unreachable by construction.
    """
    exits = decide_exits(
        portfolio_of(
            leg(753.0, qty=4, cost_basis=248.0, unrealized_pl=500.0),
            leg(748.0, qty=-4, cost_basis=0.0, unrealized_pl=0.0),
        ),
        config,
    )
    assert len(exits) == 1
