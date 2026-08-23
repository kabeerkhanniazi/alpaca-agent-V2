"""Shared fixtures.

Nothing here touches the network. The risk gate and spread builder are pure
functions by design, and the two nodes that do talk to Alpaca are exercised
against a fake broker so the suite runs identically at midnight on a Sunday.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from agent.config import AgentConfig


@pytest.fixture
def risk_config() -> dict:
    """The shipped risk thresholds, loaded from the real config file."""
    root = Path(__file__).resolve().parent.parent
    with open(root / "config" / "risk_config.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def options_config() -> dict:
    root = Path(__file__).resolve().parent.parent
    with open(root / "config" / "options_config.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def agent_config() -> dict:
    """The V2-only settings: the model loop and the MCP data layer.

    Kept separate from options_config because that file remains the single
    source of truth for every strategy threshold the ported modules read.
    """
    return {
        "cycle_interval_seconds": 300,
        "max_turns_per_ticker": 8,
        "cycle_timeout_seconds": 240,
        "llm": {"provider": "openrouter", "model": "test-model", "fallbacks": []},
        "chain": {"feed": "indicative", "strike_bracket_pct": 0.15, "limit": 1000},
    }


@pytest.fixture
def config(risk_config, options_config, agent_config, tmp_path) -> AgentConfig:
    """A config bound to a temp directory, so tests never touch real data files."""
    return AgentConfig(
        risk=risk_config,
        options=options_config,
        agent=agent_config,
        credentials=None,
        paths={
            "journal": tmp_path / "journal.jsonl",
            "iv_history": tmp_path / "iv_history.jsonl",
            "baseline": tmp_path / "account_baseline.json",
            "logs": tmp_path,
            "data": tmp_path,
        },
    )


@pytest.fixture
def spread() -> dict:
    """A well-formed bull put spread that passes every rule under `portfolio`.

    SPY 753/748, 9 DTE, $63 credit, $437 max loss. Modelled on a real chain
    snapshot so the numbers are internally consistent.
    """
    return {
        "type": "bull_put",
        "ticker": "SPY",
        "expiry": "2026-08-31",
        "dte": 9,
        "sell_symbol": "SPY260831P00753000",
        "buy_symbol": "SPY260831P00748000",
        "sell_strike": 753.0,
        "buy_strike": 748.0,
        "spread_width": 5.0,
        "sell_bid": 1.71,
        "sell_ask": 1.72,
        "buy_bid": 1.08,
        "buy_ask": 1.09,
        "sell_delta": -0.1938,
        "buy_delta": -0.09,
        "net_delta": 0.1038,
        "net_credit": 63.0,
        "mid_credit": 68.0,
        "max_loss": 437.0,
        "prob_profit": 80.62,
        "max_contracts": 4,
    }


@pytest.fixture
def portfolio() -> dict:
    """A clean $100k account: no positions, flat on the day, full buying power."""
    return {
        "nav": 100000.0,
        "spot": 765.55,
        "buying_power": 400000.0,
        "starting_buying_power": 400000.0,
        "daily_pnl": 0.0,
        "daily_pnl_pct": 0.0,
        "net_delta_dollars": 0.0,
        "open_positions": [],
    }


# ---- Fakes for the broker-facing nodes ------------------------------------


class FakeGreeks:
    def __init__(self, delta, gamma=0.01, theta=-0.05, vega=0.2, rho=0.01):
        self.delta = delta
        self.gamma = gamma
        self.theta = theta
        self.vega = vega
        self.rho = rho


class FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class FakeSnapshot:
    def __init__(self, delta, bid, ask, iv=0.15):
        self.greeks = FakeGreeks(delta)
        self.latest_quote = FakeQuote(bid, ask)
        self.implied_volatility = iv


def occ_symbol(root: str, expiry: date, strike: float, kind: str = "P") -> str:
    """Build an OCC contract symbol, e.g. SPY260831P00753000."""
    return f"{root}{expiry:%y%m%d}{kind}{int(round(strike * 1000)):08d}"


@pytest.fixture
def chain_expiry() -> date:
    """An expiry 9 days out — comfortably inside the 7-14 DTE window."""
    return date.today() + timedelta(days=9)


@pytest.fixture
def fake_chain(chain_expiry) -> dict:
    """A synthetic SPY put chain spanning the delta range and beyond.

    Deltas run from -0.02 far out of the money up to -0.30 near the money, so
    the calculator's delta filter has both sides to reject.
    """
    spot = 765.0
    chain = {}
    for offset in range(2, 40):
        strike = float(spot - offset)
        # Roughly linear in this narrow band; good enough to test filtering.
        delta = -max(0.01, 0.32 - offset * 0.0085)
        mid = max(0.05, 3.2 - offset * 0.075)
        chain[occ_symbol("SPY", chain_expiry, strike)] = FakeSnapshot(
            delta=round(delta, 4), bid=round(mid - 0.01, 2), ask=round(mid + 0.01, 2)
        )
    return chain
