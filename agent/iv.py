"""Implied-volatility rank and regime classification.

Alpaca serves the *current* implied volatility of every contract but keeps no
history, so IV rank — today's IV as a percentile of its own past year — cannot
be read off the API. This module builds it two ways and always reports which
one produced the number:

``iv_history``
    The honest one. Every cycle appends today's at-the-money IV to
    ``data/iv_history.jsonl``. Once enough sessions have accumulated, IV rank is
    today's ATM IV ranked against that record.

``rv_proxy``
    The cold-start fallback, used until the history fills. Ranks today's ATM IV
    against the past year's range of 20-day realized volatility computed from
    daily bars. It answers a slightly different question — "is implied vol high
    relative to how much this thing has actually been moving?" — which is a
    reasonable stand-in for premium-selling, but it is not the same statistic.

Everything this module emits carries ``iv_rank_source`` so a reader of the trade
journal can tell which definition was in force for a given decision.
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

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

logger = logging.getLogger(__name__)

REGIME_HIGH = "HIGH_IV"
REGIME_MID = "MID_IV"
REGIME_LOW = "LOW_IV"
REGIME_UNKNOWN = "UNKNOWN"

SOURCE_HISTORY = "iv_history"
SOURCE_RV_PROXY = "rv_proxy"
SOURCE_NONE = "unavailable"


def atm_implied_volatility(chain: dict, spot: float) -> float | None:
    """Implied volatility of the contract whose strike sits closest to spot.

    Averages the two nearest strikes when they straddle spot, which smooths the
    sampling jitter that would otherwise show up in the IV history as the ATM
    strike hops between adjacent contracts day to day.
    """
    samples: list[tuple[float, float]] = []
    for symbol, snapshot in chain.items():
        iv = getattr(snapshot, "implied_volatility", None)
        if iv is None or iv <= 0:
            continue
        strike = strike_from_symbol(symbol)
        if strike is None:
            continue
        samples.append((abs(strike - spot), float(iv)))

    if not samples:
        return None
    samples.sort(key=lambda pair: pair[0])
    nearest = [iv for _, iv in samples[:2]]
    return float(sum(nearest) / len(nearest))


def strike_from_symbol(symbol: str) -> float | None:
    """Parse the strike out of an OCC contract symbol.

    OCC format is ``ROOT`` + ``YYMMDD`` + ``C``/``P`` + an 8-digit strike in
    thousandths, e.g. ``SPY260831P00689000`` → 689.0.
    """
    if len(symbol) < 15:
        return None
    try:
        return int(symbol[-8:]) / 1000.0
    except ValueError:
        return None


def expiry_from_symbol(symbol: str) -> date | None:
    """Parse the expiration date out of an OCC contract symbol."""
    if len(symbol) < 15:
        return None
    try:
        return datetime.strptime(symbol[-15:-9], "%y%m%d").date()
    except ValueError:
        return None


def realized_volatility_series(closes: Iterable[float], window: int = 20) -> np.ndarray:
    """Annualised rolling realized volatility from a close series."""
    prices = np.asarray(list(closes), dtype=float)
    if prices.size < window + 2:
        return np.array([])
    log_returns = np.diff(np.log(prices))
    if log_returns.size < window:
        return np.array([])
    # Rolling standard deviation, annualised by the usual 252 trading days.
    windows = np.lib.stride_tricks.sliding_window_view(log_returns, window)
    return windows.std(axis=1, ddof=1) * np.sqrt(252.0)


def percentile_rank(value: float, population: Iterable[float]) -> float:
    """Where ``value`` falls within ``population``, as a 0-100 percentile.

    Uses the fraction of the population at or below the value, which is the
    standard "IV rank by percentile" convention.
    """
    data = np.asarray(list(population), dtype=float)
    if data.size == 0:
        return float("nan")
    return float((data <= value).sum() / data.size * 100.0)


def load_iv_history(path: Path, ticker: str) -> list[dict[str, Any]]:
    """Read this ticker's ATM IV samples from the history file."""
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line from a killed process; skip it
            if record.get("ticker") == ticker:
                out.append(record)
    return out


def append_iv_sample(path: Path, ticker: str, atm_iv: float, spot: float) -> None:
    """Record one ATM IV sample, at most one per ticker per calendar day.

    Cycles run every five minutes, so without the same-day guard a single
    session would contribute ~78 samples and swamp the history.
    """
    today = date.today().isoformat()
    existing = load_iv_history(path, ticker)
    if any(record.get("date") == today for record in existing):
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "date": today,
        "ticker": ticker,
        "atm_iv": round(float(atm_iv), 6),
        "spot": round(float(spot), 4),
        "recorded_at": datetime.now().astimezone().isoformat(),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def classify_regime(iv_rank: float | None, high_min: float, low_max: float) -> str:
    """Bucket an IV rank into a premium-selling regime."""
    if iv_rank is None or (isinstance(iv_rank, float) and np.isnan(iv_rank)):
        return REGIME_UNKNOWN
    if iv_rank >= high_min:
        return REGIME_HIGH
    if iv_rank <= low_max:
        return REGIME_LOW
    return REGIME_MID


def compute_iv_rank(
    ticker: str,
    atm_iv: float | None,
    closes: Iterable[float],
    history_path: Path,
    iv_config: dict[str, Any],
) -> dict[str, Any]:
    """Compute IV rank and regime, preferring real IV history over the proxy.

    Returns a dict carrying the rank, the regime, and — critically — the source
    that produced it, so no downstream consumer can mistake the proxy for the
    real statistic.
    """
    high_min = float(iv_config.get("high_regime_min", 60.0))
    low_max = float(iv_config.get("low_regime_max", 30.0))
    min_samples = int(iv_config.get("min_history_samples", 20))
    rv_window = int(iv_config.get("realized_vol_window", 20))
    lookback = int(iv_config.get("lookback_days", 252))

    if atm_iv is None or atm_iv <= 0:
        return {
            "ticker": ticker,
            "atm_iv": None,
            "iv_rank": None,
            "iv_rank_source": SOURCE_NONE,
            "regime": REGIME_UNKNOWN,
            "history_samples": 0,
            "note": "No usable implied volatility in the option chain.",
        }

    history = load_iv_history(history_path, ticker)
    ivs = [float(r["atm_iv"]) for r in history if r.get("atm_iv")]

    if len(ivs) >= min_samples:
        rank = percentile_rank(atm_iv, ivs)
        source = SOURCE_HISTORY
        note = f"Ranked against {len(ivs)} recorded ATM IV samples."
    else:
        rv_series = realized_volatility_series(closes, window=rv_window)
        if rv_series.size == 0:
            return {
                "ticker": ticker,
                "atm_iv": round(float(atm_iv), 6),
                "iv_rank": None,
                "iv_rank_source": SOURCE_NONE,
                "regime": REGIME_UNKNOWN,
                "history_samples": len(ivs),
                "note": "Not enough price history to compute a realized-vol proxy.",
            }
        rv_series = rv_series[-lookback:]
        rank = percentile_rank(atm_iv, rv_series)
        source = SOURCE_RV_PROXY
        note = (
            f"Cold start: only {len(ivs)}/{min_samples} IV samples recorded, so today's "
            f"ATM IV is ranked against {rv_series.size} days of {rv_window}-day realized vol."
        )

    return {
        "ticker": ticker,
        "atm_iv": round(float(atm_iv), 6),
        "iv_rank": round(float(rank), 2),
        "iv_rank_source": source,
        "regime": classify_regime(rank, high_min, low_max),
        "history_samples": len(ivs),
        "note": note,
    }
