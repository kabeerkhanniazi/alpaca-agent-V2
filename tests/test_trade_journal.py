"""Journal writing, reading, and the analytics the dashboard depends on."""

from __future__ import annotations

import json

import pytest

from agent.journal import TradeJournal


@pytest.fixture
def journal(tmp_path):
    return TradeJournal(tmp_path / "journal.jsonl")


def test_every_line_is_valid_json(journal):
    for i in range(5):
        journal.write("test_event", {"i": i})
    with open(journal.path) as handle:
        for line in handle:
            json.loads(line)


def test_entries_carry_a_timestamp_and_type(journal):
    record = journal.write("test_event", {"ticker": "SPY"})
    assert record["timestamp"]
    assert record["event_type"] == "test_event"


def test_writes_append_rather_than_overwrite(journal):
    journal.write("a", {})
    journal.write("b", {})
    assert len(list(journal.read_all())) == 2


def test_a_write_failure_never_raises(tmp_path):
    """A disk problem must not take down an unattended trading agent."""
    journal = TradeJournal(tmp_path / "sub" / "journal.jsonl")
    journal.path = tmp_path  # a directory: writing here will fail
    journal.write("test_event", {})  # must not raise


def test_a_torn_line_is_skipped_on_read(journal):
    journal.write("good", {})
    with open(journal.path, "a") as handle:
        handle.write('{"broken": ')
    journal.write("also_good", {})
    assert len(list(journal.read_all())) == 2


def test_reading_a_missing_file_yields_nothing(tmp_path):
    assert list(TradeJournal(tmp_path / "nope.jsonl").read_all()) == []


def test_recent_events_come_back_newest_first(journal):
    for i in range(5):
        journal.write("event", {"i": i})
    recent = journal.load_recent(limit=3)
    assert [e["i"] for e in recent] == [4, 3, 2]


def test_recent_events_filter_by_ticker(journal):
    journal.write("event", {"ticker": "SPY"})
    journal.write("event", {"ticker": "QQQ"})
    assert len(journal.load_recent(ticker="SPY")) == 1


def test_recent_events_filter_by_type(journal):
    journal.write("trade_approved", {"ticker": "SPY"})
    journal.write("trade_rejected", {"ticker": "SPY"})
    assert len(journal.load_recent(event_types=["trade_rejected"])) == 1


def test_gate_decisions_record_the_failing_rules(journal):
    journal.log_gate_decision(
        "run1", "SPY", {"net_credit": 40}, False, "nope",
        [{"rule": "R1_delta", "passed": True}, {"rule": "R4_min_premium", "passed": False}],
    )
    event = next(journal.read_all())
    assert event["event_type"] == "trade_rejected"
    assert event["failing_rules"] == ["R4_min_premium"]


def test_win_rate_counts_closed_positions_only(journal):
    """Open positions have no realized outcome and must not inflate the number."""
    journal.write("position_exit", {"ticker": "SPY", "execution": {"realized_pnl": 50.0}})
    journal.write("position_exit", {"ticker": "SPY", "execution": {"realized_pnl": 30.0}})
    journal.write("position_exit", {"ticker": "SPY", "execution": {"realized_pnl": -80.0}})
    journal.write("order_filled", {"ticker": "SPY", "execution": {"contracts": 1}})
    assert journal.compute_win_rate() == pytest.approx(66.67, abs=0.01)


def test_win_rate_is_none_before_anything_closes(journal):
    journal.log_gate_decision("r", "SPY", {}, True, "ok", [])
    assert journal.compute_win_rate() is None


def test_realized_pnl_sums_closed_trades(journal):
    journal.write("position_exit", {"ticker": "SPY", "execution": {"realized_pnl": 120.0}})
    journal.write("position_exit", {"ticker": "SPY", "execution": {"realized_pnl": -45.0}})
    assert journal.compute_total_pnl() == pytest.approx(75.0)


def test_stats_track_the_approval_rate(journal):
    journal.log_gate_decision("r1", "SPY", {}, True, "ok", [])
    journal.log_gate_decision("r2", "SPY", {}, False, "no", [{"rule": "R1_delta", "passed": False}])
    journal.log_gate_decision("r3", "SPY", {}, False, "no", [{"rule": "R1_delta", "passed": False}])
    assert journal.compute_stats()["approval_rate"] == pytest.approx(33.33, abs=0.01)


def test_stats_rank_the_most_common_rejection_reason(journal):
    """Directly useful for tuning: which rule is doing the most work?"""
    for _ in range(3):
        journal.log_gate_decision("r", "SPY", {}, False, "no", [{"rule": "R4_min_premium", "passed": False}])
    journal.log_gate_decision("r", "SPY", {}, False, "no", [{"rule": "R1_delta", "passed": False}])

    by_rule = journal.compute_stats()["rejections_by_rule"]
    assert list(by_rule)[0] == "R4_min_premium"
    assert by_rule["R4_min_premium"] == 3


def test_credits_scale_by_contract_count(journal):
    journal.log_execution("r", "SPY", {"net_credit": 60.0, "max_loss": 440.0},
                          {"success": True, "status": "filled", "contracts": 4})
    stats = journal.compute_stats()
    assert stats["total_credit"] == pytest.approx(240.0)
    assert stats["total_notional_at_risk"] == pytest.approx(1760.0)


def test_stats_by_ticker_are_isolated(journal):
    journal.write("position_exit", {"ticker": "SPY", "execution": {"realized_pnl": 100.0}})
    journal.write("position_exit", {"ticker": "QQQ", "execution": {"realized_pnl": -50.0}})
    assert journal.compute_total_pnl("SPY") == pytest.approx(100.0)
    assert journal.compute_total_pnl("QQQ") == pytest.approx(-50.0)


def test_a_failed_order_is_logged_as_a_failure(journal):
    journal.log_execution("r", "SPY", {}, {"success": False, "error": "rejected"})
    assert next(journal.read_all())["event_type"] == "order_failed"


def test_stats_on_an_empty_journal_do_not_crash(journal):
    stats = journal.compute_stats()
    assert stats["total_events"] == 0
    assert stats["win_rate"] is None
    assert stats["realized_pnl"] == 0.0


def test_a_dry_run_is_not_recorded_as_a_submitted_order(journal):
    """A simulated trade is not a trade. It must not inflate the headline numbers."""
    journal.log_execution("r", "SPY", {"net_credit": 60.0, "max_loss": 440.0},
                          {"success": True, "status": "dry_run", "dry_run": True, "contracts": 4})
    stats = journal.compute_stats()
    assert stats["orders_submitted"] == 0
    assert stats["orders_filled"] == 0
    assert stats["dry_runs"] == 1
    assert stats["total_credit"] == 0.0


def test_dry_runs_and_real_orders_are_counted_separately(journal):
    journal.log_execution("r", "SPY", {"net_credit": 60.0, "max_loss": 440.0},
                          {"success": True, "status": "dry_run", "dry_run": True, "contracts": 4})
    journal.log_execution("r", "SPY", {"net_credit": 60.0, "max_loss": 440.0},
                          {"success": True, "status": "filled", "dry_run": False, "contracts": 2})
    stats = journal.compute_stats()
    assert stats["dry_runs"] == 1
    assert stats["orders_filled"] == 1
    assert stats["total_credit"] == pytest.approx(120.0)
