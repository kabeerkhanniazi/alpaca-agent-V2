"""IV rank, the realized-volatility cold-start proxy, and OCC symbol parsing."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from agent import iv as ivmod


# ------------------------------------------------------ symbol parsing

def test_parses_strike_from_occ_symbol():
    assert ivmod.strike_from_symbol("SPY260831P00689000") == 689.0


def test_parses_fractional_strike():
    assert ivmod.strike_from_symbol("SPY260831P00689500") == 689.5


def test_parses_expiry_from_occ_symbol():
    assert ivmod.expiry_from_symbol("SPY260831P00689000") == date(2026, 8, 31)


def test_malformed_symbols_return_none():
    assert ivmod.strike_from_symbol("SPY") is None
    assert ivmod.expiry_from_symbol("SPY") is None


# ------------------------------------------------------- percentile rank

def test_percentile_rank_at_the_top():
    assert ivmod.percentile_rank(10, [1, 2, 3, 4, 5]) == 100.0


def test_percentile_rank_at_the_bottom():
    assert ivmod.percentile_rank(0, [1, 2, 3, 4, 5]) == 0.0


def test_percentile_rank_in_the_middle():
    assert ivmod.percentile_rank(3, [1, 2, 3, 4, 5]) == 60.0


def test_percentile_rank_of_empty_population_is_nan():
    assert np.isnan(ivmod.percentile_rank(1, []))


# ------------------------------------------------------- realized vol

def test_realized_volatility_is_annualised():
    """A steady 1% daily move annualises to roughly 16% vol."""
    prices = [100.0]
    for i in range(60):
        prices.append(prices[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    series = ivmod.realized_volatility_series(prices, window=20)
    assert series.size > 0
    assert 0.10 < series[-1] < 0.25


def test_flat_prices_give_zero_volatility():
    series = ivmod.realized_volatility_series([100.0] * 40, window=20)
    assert series[-1] == pytest.approx(0.0, abs=1e-9)


def test_too_little_history_returns_empty():
    assert ivmod.realized_volatility_series([100, 101, 102], window=20).size == 0


# ----------------------------------------------------------- regimes

@pytest.mark.parametrize("rank,expected", [
    (85.0, ivmod.REGIME_HIGH),
    (60.0, ivmod.REGIME_HIGH),
    (45.0, ivmod.REGIME_MID),
    (30.0, ivmod.REGIME_LOW),
    (5.0, ivmod.REGIME_LOW),
])
def test_regime_classification(rank, expected):
    assert ivmod.classify_regime(rank, 60.0, 30.0) == expected


def test_unknown_rank_gives_unknown_regime():
    assert ivmod.classify_regime(None, 60.0, 30.0) == ivmod.REGIME_UNKNOWN
    assert ivmod.classify_regime(float("nan"), 60.0, 30.0) == ivmod.REGIME_UNKNOWN


# ------------------------------------------------------------- ATM IV

def test_atm_iv_picks_the_nearest_strikes(chain_expiry):
    from tests.conftest import FakeSnapshot, occ_symbol

    chain = {
        occ_symbol("SPY", chain_expiry, 700.0): FakeSnapshot(-0.05, 1, 2, iv=0.30),
        occ_symbol("SPY", chain_expiry, 764.0): FakeSnapshot(-0.48, 1, 2, iv=0.12),
        occ_symbol("SPY", chain_expiry, 766.0): FakeSnapshot(-0.52, 1, 2, iv=0.14),
    }
    # Spot 765 sits between 764 and 766, so the answer is their average.
    assert ivmod.atm_implied_volatility(chain, 765.0) == pytest.approx(0.13, abs=1e-6)


def test_atm_iv_ignores_contracts_without_iv(chain_expiry):
    from tests.conftest import FakeSnapshot, occ_symbol

    snapshot = FakeSnapshot(-0.5, 1, 2)
    snapshot.implied_volatility = None
    assert ivmod.atm_implied_volatility({occ_symbol("SPY", chain_expiry, 765.0): snapshot}, 765.0) is None


def test_atm_iv_of_empty_chain_is_none():
    assert ivmod.atm_implied_volatility({}, 765.0) is None


# ------------------------------------------------------- history file

def test_appends_one_sample(tmp_path):
    path = tmp_path / "iv.jsonl"
    ivmod.append_iv_sample(path, "SPY", 0.15, 765.0)
    assert len(ivmod.load_iv_history(path, "SPY")) == 1


def test_only_one_sample_per_ticker_per_day(tmp_path):
    """Cycles run every five minutes; without this guard one day would log ~78."""
    path = tmp_path / "iv.jsonl"
    for _ in range(10):
        ivmod.append_iv_sample(path, "SPY", 0.15, 765.0)
    assert len(ivmod.load_iv_history(path, "SPY")) == 1


def test_history_is_separated_by_ticker(tmp_path):
    path = tmp_path / "iv.jsonl"
    ivmod.append_iv_sample(path, "SPY", 0.15, 765.0)
    ivmod.append_iv_sample(path, "QQQ", 0.22, 690.0)
    assert len(ivmod.load_iv_history(path, "SPY")) == 1
    assert len(ivmod.load_iv_history(path, "QQQ")) == 1


def test_a_torn_final_line_is_skipped(tmp_path):
    """A process killed mid-write must not poison every later read."""
    path = tmp_path / "iv.jsonl"
    ivmod.append_iv_sample(path, "SPY", 0.15, 765.0)
    with open(path, "a") as handle:
        handle.write('{"ticker": "SPY", "atm_iv": 0.1')  # truncated
    assert len(ivmod.load_iv_history(path, "SPY")) == 1


# --------------------------------------------------------- IV rank

def test_cold_start_uses_the_realized_vol_proxy(tmp_path):
    closes = list(np.linspace(100, 120, 300) + np.random.default_rng(0).normal(0, 1, 300))
    result = ivmod.compute_iv_rank("SPY", 0.15, closes, tmp_path / "iv.jsonl", {})
    assert result["iv_rank_source"] == ivmod.SOURCE_RV_PROXY
    assert result["iv_rank"] is not None


def test_switches_to_real_history_once_enough_samples_exist(tmp_path):
    path = tmp_path / "iv.jsonl"
    records = [
        {"date": f"2026-01-{i:02d}", "ticker": "SPY", "atm_iv": 0.10 + i * 0.002}
        for i in range(1, 26)
    ]
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    result = ivmod.compute_iv_rank("SPY", 0.15, [100.0] * 300, path, {"min_history_samples": 20})
    assert result["iv_rank_source"] == ivmod.SOURCE_HISTORY
    assert result["history_samples"] == 25


def test_missing_iv_reports_unavailable_not_a_guess(tmp_path):
    result = ivmod.compute_iv_rank("SPY", None, [100.0] * 300, tmp_path / "iv.jsonl", {})
    assert result["iv_rank_source"] == ivmod.SOURCE_NONE
    assert result["iv_rank"] is None
    assert result["regime"] == ivmod.REGIME_UNKNOWN


def test_every_result_declares_its_source(tmp_path):
    """The proxy must never be mistakable for a real IV rank."""
    result = ivmod.compute_iv_rank("SPY", 0.15, list(np.linspace(100, 120, 300)), tmp_path / "iv.jsonl", {})
    assert "iv_rank_source" in result
    assert result["iv_rank_source"] in (ivmod.SOURCE_HISTORY, ivmod.SOURCE_RV_PROXY, ivmod.SOURCE_NONE)
    assert "note" in result
