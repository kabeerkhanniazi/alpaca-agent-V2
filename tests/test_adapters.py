"""MCP JSON to the shapes the ported modules expect.

Fixtures here are **real captured responses** from the live Alpaca MCP server,
recorded during Stage 1 and stored in `spikes/`. Writing this against invented
JSON would only prove the adapter agrees with my assumptions; against captured
bytes it proves the adapter agrees with Alpaca.

The last section is the Stage 4 gate proper: real MCP JSON goes in one end and
comes out the other as candidates, spreads, and a nine-rule verdict, with the
ported logic untouched.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from agent.adapters import (
    BracketDiagnosis,
    adapt_account,
    bars_request,
    adapt_bars,
    adapt_chain,
    adapt_clock,
    adapt_positions,
    adapt_spot,
    chain_request,
    diagnose_bracket,
    option_positions,
    order_request,
    strike_bracket,
)
from agent.options_calculator import calculate_options_opportunities, index_chain
from agent.risk_gate import risk_gate_check
from agent.spread_builder import build_spreads

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A contract inside the -0.20..-0.15 target window with a two-sided quote.
TARGET_CONTRACT = "SPY260831P00750000"


@pytest.fixture(scope="module")
def captured():
    """Real responses from the live MCP server.

    Captured by `spikes/capture_fixtures.py`, which builds its chain request
    with `agent.adapters.chain_request` — so the fixture and the production
    request path cannot drift apart.
    """
    return json.loads((FIXTURES / "mcp_responses.json").read_text())


@pytest.fixture(scope="module")
def captured_chain(captured):
    return captured["get_option_chain"]


# ------------------------------------------------------------------ account


def test_account_money_fields_become_numbers(captured):
    """Rules 2, 3 and 9 do arithmetic on these; Alpaca sends them as strings."""
    account = adapt_account(captured["get_account_info"])

    assert account.equity == 100000.0
    assert isinstance(account.equity, float)
    assert isinstance(account.buying_power, float)
    assert isinstance(account.last_equity, float)
    # A string here would silently string-multiply rather than raise.
    assert account.equity * 0.02 == 2000.0


def test_non_money_account_fields_keep_their_types(captured):
    account = adapt_account(captured["get_account_info"])
    assert account.options_trading_level == 3
    assert account.status == "ACTIVE"
    assert account.trading_blocked is False


def test_a_missing_account_field_is_none_not_an_error(captured):
    assert adapt_account(captured["get_account_info"]).nonexistent_field is None


# -------------------------------------------------------------------- clock


def test_clock_timestamps_become_datetimes(captured):
    """The dashboard subtracts these to render a countdown."""
    clock = adapt_clock(captured["get_clock"])

    assert clock.is_open is False
    assert isinstance(clock.next_open, datetime)
    assert isinstance(clock.timestamp, datetime)
    # Both are aware, so the subtraction the dashboard does cannot raise.
    assert (clock.next_open - clock.timestamp).total_seconds() > 0


def test_sub_microsecond_precision_is_truncated_not_rejected(captured):
    """Alpaca sends more fractional digits than fromisoformat accepts.

    The wire carries up to nine (trailing zeros are stripped, so the exact
    count varies); Python takes at most six. Asserting on the exact digit count
    would pin an incidental detail — what matters is that anything finer than
    microseconds still parses.
    """
    raw = captured["get_clock"]["timestamp"]
    fractional = raw.split(".")[1].split("-")[0].rstrip("Z")
    assert len(fractional) > 6, f"fixture no longer exercises the truncation path: {raw}"
    assert isinstance(adapt_clock(captured["get_clock"]).timestamp, datetime)


def test_a_nine_digit_timestamp_parses():
    """The maximum precision Alpaca emits, pinned explicitly."""
    parsed = adapt_clock({"timestamp": "2026-08-22T11:48:15.043465234-04:00"}).timestamp
    assert isinstance(parsed, datetime)
    assert parsed.microsecond == 43465


def test_an_unparseable_timestamp_falls_back_to_the_string():
    """A surprising format must not take down a cycle over a display field."""
    assert adapt_clock({"timestamp": "not a date"}).timestamp == "not a date"


# ---------------------------------------------------------------- positions


def test_positions_numeric_fields_become_numbers():
    positions = adapt_positions([{
        "symbol": "SPY260831P00752000", "asset_class": "us_option",
        "qty": "-4", "avg_entry_price": "1.56", "market_value": "-624",
        "cost_basis": "-624", "unrealized_pl": "12.5", "unrealized_plpc": "0.02",
        "current_price": "1.53",
    }])
    position = positions[0]
    assert position.qty == -4.0
    assert position.unrealized_pl == 12.5
    assert isinstance(position.avg_entry_price, float)


def test_an_empty_position_list_is_handled():
    assert adapt_positions([]) == []
    assert adapt_positions(None) == []


def test_option_positions_are_separated_from_shares():
    positions = adapt_positions([
        {"symbol": "SPY", "asset_class": "us_equity", "qty": "100"},
        {"symbol": "SPY260831P00752000", "asset_class": "us_option", "qty": "-4"},
    ])
    options = option_positions(positions)
    assert len(options) == 1
    assert options[0].symbol == "SPY260831P00752000"


def test_option_positions_fall_back_to_symbol_length():
    """Guards against the asset_class representation shifting under us."""
    positions = adapt_positions([{"symbol": "SPY260831P00752000", "qty": "-4"}])
    assert len(option_positions(positions)) == 1


# -------------------------------------------------------------------- chain


def test_chain_greeks_and_iv_are_reachable_by_attribute(captured_chain):
    """IV is a SIBLING of greeks in Alpaca's JSON, not a member of it."""
    chain = adapt_chain(captured_chain)
    snapshot = chain[TARGET_CONTRACT]

    assert snapshot.greeks.delta == -0.1517
    assert snapshot.implied_volatility == 0.1353
    assert snapshot.greeks.implied_volatility is None


def test_chain_quotes_use_the_long_attribute_names(captured_chain):
    """The ported code reads .bid_price/.ask_price; the wire sends bp/ap."""
    snapshot = adapt_chain(captured_chain)[TARGET_CONTRACT]
    assert snapshot.latest_quote.bid_price == 1.26
    assert snapshot.latest_quote.ask_price == 1.33


def test_a_contract_without_greeks_is_kept_and_reports_none():
    """Normal on the indicative feed. A KeyError here would kill the cycle."""
    chain = adapt_chain({"snapshots": {
        "SPY260831P00495000": {"latestQuote": {"bp": 0, "ap": 0.01}},
    }})
    snapshot = chain["SPY260831P00495000"]
    assert snapshot.greeks is None
    assert snapshot.implied_volatility is None


def test_an_empty_chain_is_an_empty_dict():
    assert adapt_chain({}) == {}
    assert adapt_chain({"snapshots": {}}) == {}


# ------------------------------------------------------------- spot and bars


def test_spot_is_read_from_the_latest_trade():
    assert adapt_spot({"trades": {"SPY": {"p": 765.55}}}) == 765.55
    assert adapt_spot({"trade": {"p": 765.55}}) == 765.55


def test_bars_get_the_column_names_the_iv_module_reads():
    frame = adapt_bars({"bars": [
        {"o": 776.26, "h": 776.74, "l": 772.51, "c": 772.62, "v": 1349886, "t": "2026-08-17"},
        {"o": 768.81, "h": 769.5, "l": 766.945, "c": 767.365, "v": 1441710, "t": "2026-08-18"},
    ]})
    assert list(frame["close"]) == [772.62, 767.365]
    assert "high" in frame.columns and "low" in frame.columns


def test_real_bars_arrive_keyed_by_symbol(captured):
    """The server returns {"bars": {"SPY": [...]}}, not a bare list."""
    frame = adapt_bars(captured["get_stock_bars"])
    assert len(frame) > 200, "a year of daily bars should survive the adapter"
    assert list(frame.columns[:3]) == ["close", "high", "low"] or "close" in frame.columns
    assert frame["close"].dtype.kind == "f"


def test_empty_bars_return_an_empty_frame_with_the_right_columns():
    frame = adapt_bars({"bars": []})
    assert frame.empty
    assert "close" in frame.columns


# ------------------------------------------------------- request construction


def test_the_chain_request_always_names_the_feed():
    """Omitting it defaults to opra, which 403s without a signed agreement."""
    args = chain_request("SPY", 765.55, feed="indicative", bracket_pct=0.15,
                         min_dte=7, max_dte=14, limit=1000, today=date(2026, 8, 22))
    assert args["feed"] == "indicative"


def test_the_chain_request_brackets_the_strikes():
    """Without a bracket, limit is spent on worthless deep-OTM strikes."""
    args = chain_request("SPY", 765.55, feed="indicative", bracket_pct=0.15,
                         min_dte=7, max_dte=14, limit=1000, today=date(2026, 8, 22))
    assert args["strike_price_gte"] < 765.55 < args["strike_price_lte"]
    assert args["expiration_date_gte"] == "2026-08-29"
    assert args["expiration_date_lte"] == "2026-09-05"


def test_the_bracket_is_symmetric_around_spot():
    low, high = strike_bracket(765.55, 0.15)
    assert low == pytest.approx(650.72, abs=0.01)
    assert high == pytest.approx(880.38, abs=0.01)


def test_the_bars_request_always_sets_an_explicit_start():
    """Omitting start returns only a handful of bars, not a lookback window.

    IV rank's rv_proxy fallback then has nothing to rank against and reports
    `unavailable` silently, which is worse than an error.
    """
    args = bars_request("SPY", today=date(2026, 8, 22))
    assert args["start"] == "2025-07-18"
    assert args["feed"] == "iex"
    assert args["timeframe"] == "1Day"


def test_the_bars_lookback_covers_a_full_trading_year():
    """252 trading days need more than 252 calendar days."""
    args = bars_request("SPY", today=date(2026, 8, 22))
    span = date(2026, 8, 22) - date.fromisoformat(args["start"])
    assert span.days >= 365


# --------------------------------------------- bracket vs market diagnosis


def _snap(delta):
    return adapt_chain({"snapshots": {"X": {"greeks": {"delta": delta},
                                            "latestQuote": {"bp": 1, "ap": 1.1}}}})["X"]


def test_a_straddling_bracket_with_no_hits_blames_the_market():
    """The bracket did its job; the market simply had nothing in the window."""
    chain = {"a": _snap(-0.30), "b": _snap(-0.05)}
    result = diagnose_bracket(chain, (-0.20, -0.15), (650.0, 880.0), 1000, 0)
    assert result.outcome == BracketDiagnosis.NO_CANDIDATES
    assert not result.is_defect


def test_a_bracket_that_never_reached_the_money_is_a_defect():
    """Every strike further OTM than the window: the request was too tight."""
    chain = {"a": _snap(-0.02), "b": _snap(-0.05)}
    result = diagnose_bracket(chain, (-0.20, -0.15), (700.0, 720.0), 1000, 0)
    assert result.outcome == BracketDiagnosis.TOO_NARROW
    assert result.is_defect
    assert "close enough to spot" in result.detail


def test_a_bracket_that_never_reached_far_enough_out_is_a_defect():
    chain = {"a": _snap(-0.45), "b": _snap(-0.60)}
    result = diagnose_bracket(chain, (-0.20, -0.15), (760.0, 780.0), 1000, 0)
    assert result.outcome == BracketDiagnosis.TOO_NARROW
    assert "far enough OTM" in result.detail


def test_a_chain_with_no_greeks_at_all_is_a_defect():
    """What an unbracketed request returns: only worthless deep-OTM strikes."""
    chain = adapt_chain({"snapshots": {"X": {"latestQuote": {"bp": 0, "ap": 0.01}}}})
    result = diagnose_bracket(chain, (-0.20, -0.15), (420.0, 520.0), 20, 0)
    assert result.outcome == BracketDiagnosis.TOO_NARROW
    assert "no Greeks" in result.detail


def test_a_truncated_response_is_a_defect():
    """Hitting the limit means the target strikes may lie beyond what came back."""
    chain = {str(i): _snap(-0.02) for i in range(20)}
    result = diagnose_bracket(chain, (-0.20, -0.15), (650.0, 880.0), 20, 0)
    assert result.outcome == BracketDiagnosis.TOO_NARROW
    assert "limit" in result.detail


def test_finding_candidates_is_simply_ok():
    result = diagnose_bracket({"a": _snap(-0.17)}, (-0.20, -0.15), (650.0, 880.0), 1000, 3)
    assert result.outcome == BracketDiagnosis.OK
    assert not result.is_defect


def test_the_diagnosis_journals_what_it_observed():
    """Widening the bracket should be a config edit, not an investigation."""
    entry = diagnose_bracket({"a": _snap(-0.02)}, (-0.20, -0.15), (700.0, 720.0), 1000, 0).to_journal()
    assert entry["outcome"] == BracketDiagnosis.TOO_NARROW
    assert entry["bracket"] == [700.0, 720.0]
    assert entry["observed_delta_range"] == [-0.02, -0.02]
    assert entry["delta_window"] == [-0.20, -0.15]


# ------------------------------------------------------ ★ the Stage 4 gate


def test_captured_mcp_json_drives_the_ported_pipeline_end_to_end(
    captured, captured_chain, config
):
    """Real MCP JSON in, a nine-rule verdict out, with the ported logic untouched.

    This is what Stage 4's gate asks for: the ported modules running against
    adapter-produced data rather than hand-built fixtures.
    """
    as_of = date(2026, 8, 22)
    spot = 765.55

    chain = adapt_chain(captured_chain)
    account = adapt_account(captured["get_account_info"])

    candidates = calculate_options_opportunities(chain, "SPY", spot, config, as_of=as_of)
    assert candidates, "the adapter produced no candidates from a real chain"

    spreads = build_spreads(candidates, index_chain(chain, as_of=as_of),
                            account.equity, config)
    assert spreads, "the adapter produced no spreads from real candidates"

    top = spreads[0]
    assert top["net_credit"] > 0
    assert top["max_loss"] > 0
    assert top["sell_strike"] > top["buy_strike"]

    verdict = risk_gate_check(
        top,
        {
            "nav": account.equity,
            "buying_power": account.buying_power,
            "starting_buying_power": account.buying_power,
            "daily_pnl_pct": 0.0,
            "net_delta_dollars": 0.0,
            "open_positions": [],
        },
        config,
    )
    # Every rule must have been evaluated, whatever the verdict.
    assert len(verdict.checks) >= 9
    assert isinstance(verdict.approved, bool)


def test_the_adapter_preserves_greeks_precision_into_rule_one(captured_chain, config):
    """Rule 1 reads short-leg delta; a lossy adapter would shift the boundary."""
    chain = adapt_chain(captured_chain)
    candidates = calculate_options_opportunities(
        chain, "SPY", 765.55, config, as_of=date(2026, 8, 22)
    )
    for candidate in candidates:
        assert abs(candidate["delta"]) <= config.max_abs_delta


# ------------------------------------------------------------- order request


def test_a_credit_spread_submits_a_negative_limit_price():
    """Confirmed from the live schema: negative = credit, positive = debit."""
    order = order_request(
        sell_symbol="SPY260831P00752000", buy_symbol="SPY260831P00747000",
        contracts=4, limit_price=-0.53, client_order_id="oa-SPY-abc",
    )
    assert order["limit_price"] == "-0.53"
    assert float(order["limit_price"]) < 0


def test_a_positive_limit_price_is_refused_outright():
    """Submitting this would pay to open a position that should collect."""
    with pytest.raises(ValueError, match="NEGATIVE"):
        order_request(sell_symbol="A", buy_symbol="B", contracts=1,
                      limit_price=0.53, client_order_id="x")


def test_every_scalar_in_the_order_is_a_string():
    """The MCP schema types qty, limit_price and ratio_qty as strings."""
    order = order_request(sell_symbol="A", buy_symbol="B", contracts=4,
                          limit_price=-0.53, client_order_id="x")
    assert isinstance(order["qty"], str)
    assert isinstance(order["limit_price"], str)
    for leg in order["legs"]:
        assert isinstance(leg["ratio_qty"], str)


def test_the_order_is_one_mleg_with_both_legs():
    """Legging in separately risks a fill on the short side alone — a naked put."""
    order = order_request(sell_symbol="SELL", buy_symbol="BUY", contracts=1,
                          limit_price=-0.53, client_order_id="x")
    assert order["order_class"] == "mleg"
    assert len(order["legs"]) == 2
    assert order["legs"][0]["side"] == "sell"
    assert order["legs"][0]["position_intent"] == "sell_to_open"
    assert order["legs"][1]["side"] == "buy"
    assert order["legs"][1]["position_intent"] == "buy_to_open"


def test_options_orders_are_day_only():
    """The schema accepts no other time_in_force for options."""
    order = order_request(sell_symbol="A", buy_symbol="B", contracts=1,
                          limit_price=-0.53, client_order_id="x")
    assert order["time_in_force"] == "day"
