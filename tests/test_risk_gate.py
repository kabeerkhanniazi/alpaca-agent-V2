"""The risk gate's test suite.

Every one of the nine rules gets a passing case, a failing case, and — where
the rule has a numeric threshold — a boundary case sitting exactly on the
limit. Boundaries are where off-by-one risk logic actually bites: a gate that
rejects a trade at exactly 0.20 delta when the config says "<= 0.20" is wrong in
a way that only shows up in production.

This file is the reason the "no LLM in the risk gate" constraint is worth
having. None of these assertions would be possible against a model's judgement.
"""

from __future__ import annotations

import pytest

from agent.risk_gate import risk_gate_check


def check_for(result, rule: str):
    """Pull one rule's check out of a result."""
    matches = [c for c in result.checks if c.rule == rule]
    assert matches, f"{rule} was not evaluated at all"
    return matches[0]


# ---------------------------------------------------------------- baseline

def test_clean_spread_is_approved(spread, portfolio, config):
    result = risk_gate_check(spread, portfolio, config)
    assert result.approved, result.reason
    assert result.contracts == 4
    assert result.failing_rules == []
    assert "APPROVED" in result.reason


def test_all_nine_rules_are_always_evaluated(spread, portfolio, config):
    """Even a trade that fails early must report on every rule."""
    doomed = {**spread, "sell_delta": -0.9, "net_credit": 1.0, "dte": 100}
    result = risk_gate_check(doomed, portfolio, config)
    assert len({c.rule for c in result.checks}) == 9
    # And it should name every distinct thing that is wrong, not just the first.
    assert len(result.failing_rules) >= 3


# ------------------------------------------------------- R1: delta cap

def test_r1_delta_within_cap_passes(spread, portfolio, config):
    result = risk_gate_check({**spread, "sell_delta": -0.15}, portfolio, config)
    assert check_for(result, "R1_delta").passed


def test_r1_delta_above_cap_fails(spread, portfolio, config):
    result = risk_gate_check({**spread, "sell_delta": -0.35}, portfolio, config)
    assert not check_for(result, "R1_delta").passed
    assert not result.approved


def test_r1_delta_exactly_at_cap_passes(spread, portfolio, config):
    """0.20 is the cap and the rule is 'at most', so exactly 0.20 is allowed."""
    result = risk_gate_check({**spread, "sell_delta": -0.20}, portfolio, config)
    assert check_for(result, "R1_delta").passed


def test_r1_uses_absolute_value(spread, portfolio, config):
    """Puts carry negative delta; the cap is on magnitude, not sign."""
    negative = risk_gate_check({**spread, "sell_delta": -0.18}, portfolio, config)
    positive = risk_gate_check({**spread, "sell_delta": 0.18}, portfolio, config)
    assert check_for(negative, "R1_delta").passed
    assert check_for(positive, "R1_delta").passed


# --------------------------------------------------- R2: notional budget

def test_r2_sizes_position_to_the_loss_budget(spread, portfolio, config):
    """2% of $100k is $2,000; at $437 max loss that is 4 contracts."""
    result = risk_gate_check(spread, portfolio, config)
    assert result.contracts == 4
    assert check_for(result, "R2_notional").passed


def test_r2_rejects_when_one_contract_exceeds_budget(spread, portfolio, config):
    """A $2,500 max loss cannot fit a $2,000 budget even once."""
    result = risk_gate_check({**spread, "max_loss": 2500.0}, portfolio, config)
    assert not check_for(result, "R2_notional").passed
    assert not result.approved
    assert result.contracts == 0


def test_r2_boundary_max_loss_exactly_at_budget(spread, portfolio, config):
    """A max loss of exactly $2,000 allows exactly one contract."""
    result = risk_gate_check({**spread, "max_loss": 2000.0}, portfolio, config)
    assert check_for(result, "R2_notional").passed
    assert result.contracts == 1


def test_r2_scales_with_account_size(spread, portfolio, config):
    """Halving NAV halves the budget and so halves the position."""
    small = risk_gate_check(spread, {**portfolio, "nav": 50000.0}, config)
    assert small.contracts == 2


def test_r2_respects_an_explicit_contract_request(spread, portfolio, config):
    result = risk_gate_check(spread, portfolio, config, requested_contracts=2)
    assert result.contracts == 2
    assert result.approved


# ------------------------------------------------- R3: portfolio delta

def test_r3_passes_on_an_empty_book(spread, portfolio, config):
    result = risk_gate_check(spread, portfolio, config)
    assert check_for(result, "R3_portfolio_delta").passed


def test_r3_sizes_down_to_fit_remaining_headroom(spread, portfolio, config):
    """With the book part-way to the cap, the trade shrinks rather than failing.

    Sizing down is the designed behaviour: a sound spread that is merely too
    large is a sizing problem, not a risk violation.
    """
    loaded = {**portfolio, "net_delta_dollars": 25000.0}
    result = risk_gate_check(spread, loaded, config)
    assert result.approved
    assert result.contracts < 4
    assert result.contracts >= 1


def test_r3_rejects_when_headroom_cannot_fit_one_contract(spread, portfolio, config):
    exhausted = {**portfolio, "net_delta_dollars": 49_900.0}
    result = risk_gate_check(spread, exhausted, config)
    assert not result.approved
    assert "R3_portfolio_delta" in result.failing_rules


def test_r3_blames_only_itself_when_delta_binds(spread, portfolio, config):
    """A delta-bound rejection must not be misattributed to the loss budget."""
    exhausted = {**portfolio, "net_delta_dollars": 49_900.0}
    result = risk_gate_check(spread, exhausted, config)
    assert result.failing_rules == ["R3_portfolio_delta"]
    assert check_for(result, "R2_notional").passed


def test_r3_counts_existing_exposure_in_both_directions(spread, portfolio, config):
    """A short-delta book leaves room for a long-delta trade, and vice versa."""
    short_book = {**portfolio, "net_delta_dollars": -40000.0}
    result = risk_gate_check(spread, short_book, config)
    # The bull put spread is long delta, so it offsets rather than compounds.
    assert result.approved


# ------------------------------------------------------- R4: min premium

def test_r4_sufficient_credit_passes(spread, portfolio, config):
    assert check_for(risk_gate_check(spread, portfolio, config), "R4_min_premium").passed


def test_r4_thin_credit_fails(spread, portfolio, config):
    result = risk_gate_check({**spread, "net_credit": 12.0}, portfolio, config)
    assert not check_for(result, "R4_min_premium").passed
    assert not result.approved


def test_r4_boundary_exactly_at_minimum_passes(spread, portfolio, config):
    result = risk_gate_check({**spread, "net_credit": 25.0}, portfolio, config)
    assert check_for(result, "R4_min_premium").passed


def test_r4_just_below_minimum_fails(spread, portfolio, config):
    result = risk_gate_check({**spread, "net_credit": 24.99}, portfolio, config)
    assert not check_for(result, "R4_min_premium").passed


# -------------------------------------------------- R5: duplicate strike

def test_r5_no_open_positions_passes(spread, portfolio, config):
    assert check_for(risk_gate_check(spread, portfolio, config), "R5_duplicate").passed


def test_r5_same_strike_and_expiry_fails(spread, portfolio, config):
    held = {**portfolio, "open_positions": [
        {"symbol": "SPY260831P00753000", "underlying": "SPY", "expiry": "2026-08-31", "strike": 753.0},
    ]}
    result = risk_gate_check(spread, held, config)
    assert not check_for(result, "R5_duplicate").passed
    assert not result.approved


def test_r5_same_strike_different_expiry_passes(spread, portfolio, config):
    """A different expiry is a genuinely different position, not a stack."""
    held = {**portfolio, "open_positions": [
        {"symbol": "SPY260907P00753000", "underlying": "SPY", "expiry": "2026-09-07", "strike": 753.0},
    ]}
    assert check_for(risk_gate_check(spread, held, config), "R5_duplicate").passed


def test_r5_same_strike_different_underlying_passes(spread, portfolio, config):
    held = {**portfolio, "open_positions": [
        {"symbol": "QQQ260831P00753000", "underlying": "QQQ", "expiry": "2026-08-31", "strike": 753.0},
    ]}
    assert check_for(risk_gate_check(spread, held, config), "R5_duplicate").passed


def test_r5_matches_on_float_strikes_robustly(spread, portfolio, config):
    """753 and 753.0 are the same strike."""
    held = {**portfolio, "open_positions": [
        {"symbol": "SPY260831P00753000", "underlying": "SPY", "expiry": "2026-08-31", "strike": 753},
    ]}
    assert not check_for(risk_gate_check(spread, held, config), "R5_duplicate").passed


# ---------------------------------------------------------- R6/R7: DTE

@pytest.mark.parametrize("dte,expected", [(3, False), (6, False), (7, True), (10, True)])
def test_r6_min_dte(spread, portfolio, config, dte, expected):
    result = risk_gate_check({**spread, "dte": dte}, portfolio, config)
    assert check_for(result, "R6_min_dte").passed is expected


@pytest.mark.parametrize("dte,expected", [(10, True), (14, True), (15, False), (45, False)])
def test_r7_max_dte(spread, portfolio, config, dte, expected):
    result = risk_gate_check({**spread, "dte": dte}, portfolio, config)
    assert check_for(result, "R7_max_dte").passed is expected


def test_dte_window_boundaries_are_both_inclusive(spread, portfolio, config):
    """7 and 14 are both inside the window."""
    assert risk_gate_check({**spread, "dte": 7}, portfolio, config).approved
    assert risk_gate_check({**spread, "dte": 14}, portfolio, config).approved


# -------------------------------------------------------- R8: kill-switch

def test_r8_flat_day_passes(spread, portfolio, config):
    assert check_for(risk_gate_check(spread, portfolio, config), "R8_kill_switch").passed


def test_r8_small_loss_passes(spread, portfolio, config):
    result = risk_gate_check(spread, {**portfolio, "daily_pnl_pct": -0.02}, config)
    assert check_for(result, "R8_kill_switch").passed


def test_r8_large_loss_trips_the_breaker(spread, portfolio, config):
    result = risk_gate_check(spread, {**portfolio, "daily_pnl_pct": -0.08}, config)
    assert not check_for(result, "R8_kill_switch").passed
    assert not result.approved


def test_r8_boundary_exactly_five_percent_passes(spread, portfolio, config):
    """The rule is 'more than 5%', so exactly 5% is still allowed."""
    result = risk_gate_check(spread, {**portfolio, "daily_pnl_pct": -0.05}, config)
    assert check_for(result, "R8_kill_switch").passed


def test_r8_just_past_five_percent_fails(spread, portfolio, config):
    result = risk_gate_check(spread, {**portfolio, "daily_pnl_pct": -0.0501}, config)
    assert not check_for(result, "R8_kill_switch").passed


def test_r8_a_profitable_day_never_trips_the_breaker(spread, portfolio, config):
    """A big gain is not a drawdown, however large."""
    result = risk_gate_check(spread, {**portfolio, "daily_pnl_pct": 0.40}, config)
    assert check_for(result, "R8_kill_switch").passed


# ------------------------------------------------------ R9: buying power

def test_r9_ample_buying_power_passes(spread, portfolio, config):
    assert check_for(risk_gate_check(spread, portfolio, config), "R9_buying_power").passed


def test_r9_insufficient_reserve_fails(spread, portfolio, config):
    """Reserve floor is 20% of the $400k starting figure, i.e. $80,000."""
    drained = {**portfolio, "buying_power": 80_100.0}
    result = risk_gate_check(spread, drained, config)
    assert not check_for(result, "R9_buying_power").passed
    assert not result.approved


def test_r9_boundary_landing_exactly_on_the_floor_passes(spread, portfolio, config):
    """Rule is 'at least', so ending exactly at the floor is allowed."""
    # 4 contracts x $437 = $1,748 of collateral.
    drained = {**portfolio, "buying_power": 80_000.0 + 1_748.0}
    result = risk_gate_check(spread, drained, config)
    assert check_for(result, "R9_buying_power").passed


def test_r9_measures_reserve_against_the_starting_baseline(spread, portfolio, config):
    """A drawn-down account must not relax its own reserve requirement.

    If the floor were computed from *current* buying power it would shrink as
    the account shrank, permitting trades exactly when it should stop.
    """
    # $81k buying power, $1,748 of collateral, leaving $79,252.
    # Against the $80,000 baseline floor that fails, as it should. Against a
    # floor computed from current buying power (20% of $81k = $16,200) it would
    # sail through — which is exactly the bug this rule is shaped to avoid.
    drawn_down = {**portfolio, "buying_power": 81_000.0, "starting_buying_power": 400_000.0}
    result = risk_gate_check(spread, drawn_down, config)
    assert not check_for(result, "R9_buying_power").passed


# ------------------------------------------------------------ reporting

def test_rejection_reason_names_the_failing_rule(spread, portfolio, config):
    result = risk_gate_check({**spread, "net_credit": 5.0}, portfolio, config)
    assert "R4_min_premium" in result.reason
    assert "REJECTED" in result.reason


def test_every_check_reports_observed_and_limit(spread, portfolio, config):
    """A decision must be reconstructable from the journal alone."""
    result = risk_gate_check(spread, portfolio, config)
    for check in result.checks:
        if check.rule == "R5_duplicate":
            continue  # a non-numeric rule; its limit is legitimately None
        assert check.observed is not None, f"{check.rule} recorded no observed value"
        assert check.limit is not None, f"{check.rule} recorded no limit"


def test_result_serialises_for_the_journal(spread, portfolio, config):
    payload = risk_gate_check(spread, portfolio, config).to_dict()
    assert payload["approved"] is True
    assert len(payload["checks"]) == 9
    assert payload["failing_rules"] == []


def test_gate_is_deterministic(spread, portfolio, config):
    """The same inputs must always give the same answer. This is the whole point."""
    results = [risk_gate_check(spread, portfolio, config) for _ in range(20)]
    assert len({(r.approved, r.contracts, r.reason) for r in results}) == 1


def test_gate_makes_no_network_calls(spread, portfolio, config, monkeypatch):
    """A hard guarantee that the gate is pure: break sockets and it still works."""
    import socket

    def explode(*args, **kwargs):
        raise AssertionError("The risk gate must never touch the network.")

    monkeypatch.setattr(socket, "socket", explode)
    assert risk_gate_check(spread, portfolio, config).approved


def test_missing_portfolio_fields_fail_closed(spread, config):
    """An empty portfolio dict must never produce an approval."""
    assert not risk_gate_check(spread, {}, config).approved
