"""Spread construction arithmetic.

The numbers here are hand-computed rather than derived from the implementation,
so a change in the formula shows up as a failure instead of quietly redefining
what "max loss" means.
"""

from __future__ import annotations

import pytest

from agent.spread_builder import build_spreads


def make_candidate(strike=753.0, delta=-0.19, bid=1.71, ask=1.72, expiry="2026-08-31", dte=9):
    return {
        "symbol": f"SPY260831P00{int(strike * 1000):06d}",
        "ticker": "SPY",
        "strike": strike,
        "expiry": expiry,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": round((bid + ask) / 2, 4),
        "delta": delta,
        "theta": -0.05,
        "vega": 0.2,
        "gamma": 0.01,
        "iv": 0.15,
    }


def make_index(expiry="2026-08-31", strikes=None):
    """Long-leg quotes, cheaper the further out of the money."""
    strikes = strikes or [748.0, 747.0, 746.0, 745.0, 744.0, 743.0]
    index = {}
    for strike in strikes:
        distance = 753.0 - strike
        mid = max(0.05, 1.71 - distance * 0.13)
        index[f"{expiry}|{strike}"] = {
            "symbol": f"SPY260831P00{int(strike * 1000):06d}",
            "strike": strike,
            "expiry": expiry,
            "dte": 9,
            "bid": round(mid - 0.01, 2),
            "ask": round(mid + 0.01, 2),
            "delta": -0.09,
            "theta": -0.03,
            "vega": 0.1,
            "gamma": 0.008,
            "iv": 0.16,
        }
    return index


def test_builds_at_least_one_spread(config):
    spreads = build_spreads([make_candidate()], make_index(), 100000.0, config)
    assert spreads


def test_credit_uses_the_conservative_fill(config):
    """Sell at the bid, buy at the ask — never the mid on both sides."""
    candidate = make_candidate(bid=1.71, ask=1.72)
    index = make_index()
    spreads = build_spreads([candidate], index, 100000.0, config)
    five_wide = next(s for s in spreads if s["spread_width"] == 5.0)

    long_ask = index["2026-08-31|748.0"]["ask"]
    assert five_wide["net_credit"] == pytest.approx((1.71 - long_ask) * 100, abs=0.01)


def test_max_loss_is_width_minus_credit(config):
    spreads = build_spreads([make_candidate()], make_index(), 100000.0, config)
    for spread in spreads:
        expected = spread["spread_width"] * 100.0 - spread["net_credit"]
        assert spread["max_loss"] == pytest.approx(expected, abs=0.01)


def test_probability_of_profit_from_short_delta(config):
    """POP is 1 minus the short leg's delta magnitude."""
    spreads = build_spreads([make_candidate(delta=-0.19)], make_index(), 100000.0, config)
    assert all(s["prob_profit"] == pytest.approx(81.0, abs=0.01) for s in spreads)


def test_net_delta_is_long_delta_minus_short_delta(config):
    """A bull put spread is net LONG delta. The sign here must be positive.

    Getting this backwards would label a bullish position bearish and feed the
    portfolio-delta rule a number pointing the wrong way.
    """
    spreads = build_spreads([make_candidate(delta=-0.19)], make_index(), 100000.0, config)
    for spread in spreads:
        assert spread["net_delta"] > 0, "bull put spread must be net long delta"
        assert spread["net_delta"] == pytest.approx(-0.09 - (-0.19), abs=1e-6)


def test_breakeven_is_short_strike_less_credit(config):
    spreads = build_spreads([make_candidate()], make_index(), 100000.0, config)
    for spread in spreads:
        expected = spread["sell_strike"] - spread["net_credit"] / 100.0
        assert spread["breakeven"] == pytest.approx(expected, abs=0.01)


def test_max_contracts_fits_the_loss_budget(config):
    spreads = build_spreads([make_candidate()], make_index(), 100000.0, config)
    for spread in spreads:
        assert spread["max_loss"] * spread["max_contracts"] <= 2000.0


def test_spreads_over_the_loss_budget_are_dropped(config):
    """On a small account, a 5-wide spread's $500 risk exceeds a 2% budget."""
    spreads = build_spreads([make_candidate()], make_index(), 10_000.0, config)
    assert spreads == []


def test_results_are_ranked_best_first(config):
    spreads = build_spreads(
        [make_candidate(strike=753.0), make_candidate(strike=752.0, delta=-0.17, bid=1.56, ask=1.57)],
        make_index(),
        100000.0,
        config,
    )
    scores = [s["score"] for s in spreads]
    assert scores == sorted(scores, reverse=True)


def test_respects_the_top_n_cap(config):
    candidates = [
        make_candidate(strike=753.0 - i, delta=-0.19 + i * 0.005)
        for i in range(6)
    ]
    spreads = build_spreads(candidates, make_index(), 100000.0, config)
    assert len(spreads) <= int(config.spread_builder["top_n"])


def test_skips_strikes_with_no_long_leg_available(config):
    """No quote for the protective leg means no defined-risk spread. Skip it."""
    spreads = build_spreads([make_candidate()], {}, 100000.0, config)
    assert spreads == []


def test_rejects_a_spread_that_would_be_a_debit(config):
    """If the long leg costs more than the short leg pays, there is no credit."""
    index = make_index()
    for leg in index.values():
        leg["ask"] = 9.99
    assert build_spreads([make_candidate()], index, 100000.0, config) == []


def test_rejects_credit_exceeding_the_width(config):
    """A credit above the spread width implies arbitrage — in reality, a bad quote."""
    index = make_index()
    for leg in index.values():
        leg["ask"] = 0.01
    candidate = make_candidate(bid=9.00, ask=9.05)  # credit $899 on a $500 wide spread
    spreads = build_spreads([candidate], index, 100000.0, config)
    assert all(s["max_loss"] > 0 for s in spreads)


def test_every_spread_is_defined_risk(config):
    """The core safety invariant: both legs present, loss capped by the width."""
    spreads = build_spreads([make_candidate()], make_index(), 100000.0, config)
    assert spreads
    for spread in spreads:
        assert spread["buy_symbol"], "no protective leg means a naked short"
        assert spread["buy_strike"] < spread["sell_strike"]
        assert 0 < spread["max_loss"] <= spread["spread_width"] * 100.0
