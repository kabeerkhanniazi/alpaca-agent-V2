"""Chain filtering: delta window, DTE window, and quote quality."""

from __future__ import annotations

from datetime import date, timedelta

from agent.options_calculator import (
    calculate_options_opportunities,
    index_chain,
)


def test_returns_only_strikes_inside_the_delta_window(fake_chain, config):
    candidates = calculate_options_opportunities(fake_chain, "SPY", 765.0, config)
    assert candidates
    low, high = config.delta_range
    for candidate in candidates:
        assert low <= candidate["delta"] <= high


def test_filters_out_far_out_of_the_money_strikes(fake_chain, config):
    """The synthetic chain runs down to -0.01 delta; none of that should survive."""
    candidates = calculate_options_opportunities(fake_chain, "SPY", 765.0, config)
    assert all(abs(c["delta"]) >= 0.15 for c in candidates)
    assert len(candidates) < len(fake_chain)


def test_filters_out_near_the_money_strikes(fake_chain, config):
    candidates = calculate_options_opportunities(fake_chain, "SPY", 765.0, config)
    assert all(abs(c["delta"]) <= 0.20 for c in candidates)


def test_rejects_expiries_outside_the_dte_window(config, chain_expiry):
    from tests.conftest import FakeSnapshot, occ_symbol

    too_soon = date.today() + timedelta(days=2)
    too_far = date.today() + timedelta(days=60)
    chain = {
        occ_symbol("SPY", too_soon, 750.0): FakeSnapshot(-0.17, 1.70, 1.72),
        occ_symbol("SPY", too_far, 750.0): FakeSnapshot(-0.17, 1.70, 1.72),
        occ_symbol("SPY", chain_expiry, 750.0): FakeSnapshot(-0.17, 1.70, 1.72),
    }
    candidates = calculate_options_opportunities(chain, "SPY", 765.0, config)
    assert len(candidates) == 1
    assert config.min_dte <= candidates[0]["dte"] <= config.max_dte


def test_drops_strikes_with_no_bid(config, chain_expiry):
    """A contract nobody is bidding on cannot be sold at any price."""
    from tests.conftest import FakeSnapshot, occ_symbol

    chain = {occ_symbol("SPY", chain_expiry, 750.0): FakeSnapshot(-0.17, 0.0, 1.72)}
    assert calculate_options_opportunities(chain, "SPY", 765.0, config) == []


def test_drops_strikes_with_a_very_wide_quote(config, chain_expiry):
    """A 0.10/3.00 market has a fictional mid; the fill would be far worse."""
    from tests.conftest import FakeSnapshot, occ_symbol

    chain = {occ_symbol("SPY", chain_expiry, 750.0): FakeSnapshot(-0.17, 0.10, 3.00)}
    assert calculate_options_opportunities(chain, "SPY", 765.0, config) == []


def test_keeps_strikes_with_a_tight_quote(config, chain_expiry):
    from tests.conftest import FakeSnapshot, occ_symbol

    chain = {occ_symbol("SPY", chain_expiry, 750.0): FakeSnapshot(-0.17, 1.70, 1.72)}
    assert len(calculate_options_opportunities(chain, "SPY", 765.0, config)) == 1


def test_candidates_carry_full_greeks(fake_chain, config):
    candidates = calculate_options_opportunities(fake_chain, "SPY", 765.0, config)
    for candidate in candidates:
        for field in ("delta", "gamma", "theta", "vega", "iv", "bid", "ask", "mid", "credit"):
            assert candidate[field] is not None, f"{field} missing"


def test_credit_is_the_mid_in_dollars(fake_chain, config):
    candidates = calculate_options_opportunities(fake_chain, "SPY", 765.0, config)
    for candidate in candidates:
        assert candidate["credit"] == round(candidate["mid"] * 100, 2)


def test_index_covers_the_entire_chain(fake_chain, config):
    """The long leg lives outside the delta window, so the index must hold everything."""
    index = index_chain(fake_chain)
    assert len(index) == len(fake_chain)


def test_index_is_keyed_for_long_leg_lookup(fake_chain, chain_expiry):
    index = index_chain(fake_chain)
    key = f"{chain_expiry.isoformat()}|750.0"
    assert key in index
    assert index[key]["strike"] == 750.0


def test_empty_chain_yields_no_candidates(config):
    assert calculate_options_opportunities({}, "SPY", 765.0, config) == []
