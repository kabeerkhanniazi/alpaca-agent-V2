"""Unit tests for the dashboard's pure helpers.

These run without a Streamlit runtime and without a network, which is the whole
reason they were extracted out of streamlit_app.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent.dashboard_utils import (
    build_pnl_series,
    format_countdown,
    is_rejection,
    money,
    rejection_reason,
)


# ----------------------------------------------------------------- money

def test_positive_amounts_format_normally():
    assert money(1234.5) == "$1,234.50"


def test_negative_sign_sits_outside_the_currency_symbol():
    """-$212.00, not $-212.00. The latter reads as a typo."""
    assert money(-212.0) == "-$212.00"


def test_zero_is_not_signed():
    assert money(0) == "$0.00"


def test_none_renders_as_an_em_dash():
    assert money(None) == "—"


def test_decimal_places_are_configurable():
    assert money(400000, 0) == "$400,000"


# ------------------------------------------------------------- countdown

def test_multi_day_countdown_shows_days_hours_minutes():
    assert format_countdown(timedelta(days=2, hours=3, minutes=39)) == "2d 3h 39m"


def test_sub_day_countdown_drops_the_day_field():
    assert format_countdown(timedelta(hours=6, minutes=30)) == "6h 30m"


def test_sub_hour_countdown_switches_to_seconds():
    """The last stretch before the bell needs to tick visibly."""
    assert format_countdown(timedelta(minutes=4, seconds=5)) == "4m 5s"


def test_a_passed_boundary_clamps_to_now():
    """Never render a negative countdown."""
    assert format_countdown(timedelta(seconds=-30)) == "now"
    assert format_countdown(timedelta(0)) == "now"


# ------------------------------------------------------------ rejections

def test_recognises_the_journals_own_rejection_shape():
    assert is_rejection({"event_type": "trade_rejected"})


def test_recognises_the_alternate_status_shape():
    """Tolerated so a field-name mismatch cannot hide gate evidence."""
    assert is_rejection({"status": "rejected"})
    assert is_rejection({"status": "REJECTED"})


def test_approvals_are_not_rejections():
    assert not is_rejection({"event_type": "trade_approved"})
    assert not is_rejection({"event_type": "order_filled", "status": "filled"})


def test_reason_read_from_the_journal_field():
    assert rejection_reason({"reason": "R4 failed"}) == "R4 failed"


def test_reason_read_from_the_alternate_field():
    assert rejection_reason({"rejection_reason": "too thin"}) == "too thin"


def test_journal_field_wins_when_both_are_present():
    event = {"reason": "canonical", "rejection_reason": "alternate"}
    assert rejection_reason(event) == "canonical"


def test_a_missing_reason_says_so_rather_than_being_blank():
    assert rejection_reason({}) == "No reason recorded."


# -------------------------------------------------------------- P&L series

def _exit(pnl, timestamp, ticker="SPY"):
    return {
        "event_type": "position_exit",
        "timestamp": timestamp,
        "ticker": ticker,
        "execution": {"realized_pnl": pnl},
    }


def test_no_closed_positions_gives_an_empty_frame_with_columns():
    """An empty frame must still have its columns, or the chart call blows up."""
    frame = build_pnl_series([])
    assert frame.empty
    assert list(frame.columns) == ["Closed at", "Trade P&L", "Cumulative P&L", "Ticker"]


def test_pnl_accumulates_across_trades():
    frame = build_pnl_series([
        _exit(100.0, "2026-08-20T10:00:00+00:00"),
        _exit(-40.0, "2026-08-21T10:00:00+00:00"),
        _exit(60.0, "2026-08-22T10:00:00+00:00"),
    ])
    assert frame["Cumulative P&L"].tolist() == [100.0, 60.0, 120.0]


def test_out_of_order_events_are_sorted_before_accumulating():
    """The journal is append-ordered by write time, not by event time."""
    frame = build_pnl_series([
        _exit(60.0, "2026-08-22T10:00:00+00:00"),
        _exit(100.0, "2026-08-20T10:00:00+00:00"),
        _exit(-40.0, "2026-08-21T10:00:00+00:00"),
    ])
    assert frame["Trade P&L"].tolist() == [100.0, -40.0, 60.0]
    assert frame["Cumulative P&L"].tolist() == [100.0, 60.0, 120.0]


def test_only_closed_positions_count():
    """Approvals, fills and dry runs are not realized P&L."""
    frame = build_pnl_series([
        _exit(100.0, "2026-08-20T10:00:00+00:00"),
        {"event_type": "trade_approved", "timestamp": "2026-08-20T11:00:00+00:00"},
        {"event_type": "order_filled", "timestamp": "2026-08-20T12:00:00+00:00",
         "execution": {"contracts": 4}},
        {"event_type": "order_dry_run", "timestamp": "2026-08-20T13:00:00+00:00",
         "execution": {"dry_run": True}},
    ])
    assert len(frame) == 1


def test_exits_without_a_realized_figure_are_skipped():
    frame = build_pnl_series([
        _exit(100.0, "2026-08-20T10:00:00+00:00"),
        {"event_type": "position_exit", "timestamp": "2026-08-21T10:00:00+00:00",
         "execution": {"status": "failed"}},
    ])
    assert len(frame) == 1


def test_unparseable_timestamps_are_dropped_not_fatal():
    frame = build_pnl_series([
        _exit(100.0, "2026-08-20T10:00:00+00:00"),
        _exit(50.0, "not-a-timestamp"),
    ])
    assert len(frame) == 1


def test_ticker_is_carried_through_for_the_breakdown():
    frame = build_pnl_series([
        _exit(100.0, "2026-08-20T10:00:00+00:00", ticker="QQQ"),
    ])
    assert frame["Ticker"].tolist() == ["QQQ"]


def test_a_single_trade_produces_a_one_point_series():
    frame = build_pnl_series([_exit(75.0, "2026-08-20T10:00:00+00:00")])
    assert frame["Cumulative P&L"].tolist() == [75.0]
