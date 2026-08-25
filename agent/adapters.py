"""MCP JSON to the shapes the ported modules already expect.

This module replaces `broker.py` from the previous build. That module was the
single `alpaca-py` boundary; this is the single MCP boundary, and it exists so
the ported risk gate, spread builder, IV module and position manager keep
running against exactly the inputs they were tested with.

**Why an attribute view rather than dict rewriting.** The ported code reads
snapshots with ``getattr(snapshot, "implied_volatility", None)``,
``.latest_quote.bid_price``, ``.greeks.delta``. MCP returns plain JSON using
Alpaca's wire names — ``impliedVolatility`` (a *sibling* of ``greeks``, not
inside it), ``latestQuote`` with one-letter ``bp``/``ap`` keys. Resolving those
on attribute lookup keeps the mapping in one place and leaves the ported logic
untouched, which is the condition Stage 4's gate is written against.

**Three coercions that are not cosmetic.**

- Account money fields arrive as *strings* (``"100000"``). Rules 2, 3 and 9 do
  arithmetic on them, and a string reaching Rule 2 either raises or silently
  string-multiplies.
- Timestamps arrive as ISO strings with nanosecond precision. Python's
  ``fromisoformat`` accepts at most microseconds, so they are truncated first.
- Contracts on the indicative feed routinely arrive with **no** ``greeks`` key.
  That is normal for illiquid strikes, not an error, and must be skipped rather
  than raised on — Rule 1 reads short-leg delta and a KeyError there kills the
  cycle.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from .iv import expiry_from_symbol, strike_from_symbol

logger = logging.getLogger(__name__)

# Attribute name -> the wire keys that satisfy it.
_ALIASES: dict[str, tuple[str, ...]] = {
    "implied_volatility": ("impliedVolatility",),
    "latest_quote": ("latestQuote",),
    "latest_trade": ("latestTrade",),
    "daily_bar": ("dailyBar",),
    "prev_daily_bar": ("prevDailyBar",),
    "minute_bar": ("minuteBar",),
    "bid_price": ("bp",),
    "ask_price": ("ap",),
    "bid_size": ("bs",),
    "ask_size": ("as",),
}

# Fields the SDK deserialised into aware datetimes.
_DATETIME_KEYS = frozenset({
    "timestamp", "next_open", "next_close",
    "created_at", "updated_at", "submitted_at", "filled_at",
    "expired_at", "canceled_at", "failed_at", "replaced_at",
})

# Alpaca sends nine fractional digits; fromisoformat accepts three or six.
_NANOS = re.compile(r"(\.\d{6})\d+")

# Money and quantity fields that arrive as strings and are used in arithmetic.
_NUMERIC_ACCOUNT_KEYS = (
    "equity", "last_equity", "cash", "portfolio_value", "buying_power",
    "regt_buying_power", "effective_buying_power", "non_marginable_buying_power",
    "options_buying_power", "long_market_value", "short_market_value",
    "position_market_value", "initial_margin", "maintenance_margin",
    "last_maintenance_margin", "sma", "accrued_fees",
)

_NUMERIC_POSITION_KEYS = (
    "qty", "avg_entry_price", "market_value", "cost_basis", "unrealized_pl",
    "unrealized_plpc", "unrealized_intraday_pl", "unrealized_intraday_plpc",
    "current_price", "lastday_price", "change_today", "qty_available",
)


class Obj:
    """Attribute-access view over parsed MCP JSON.

    Missing keys return ``None`` rather than raising, matching how the ported
    code already treats absent fields.
    """

    __slots__ = ("_d",)

    def __init__(self, data: dict[str, Any]):
        object.__setattr__(self, "_d", data)

    def __getattr__(self, name: str) -> Any:
        d = object.__getattribute__(self, "_d")
        if name in d:
            return _coerce(name, d[name])
        for alias in _ALIASES.get(name, ()):
            if alias in d:
                return _coerce(name, d[alias])
        return None

    def __getitem__(self, key: str) -> Any:
        return _coerce(key, object.__getattribute__(self, "_d")[key])

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_d")

    def get(self, key: str, default: Any = None) -> Any:
        d = object.__getattribute__(self, "_d")
        return _coerce(key, d[key]) if key in d else default

    def to_dict(self) -> dict[str, Any]:
        return dict(object.__getattribute__(self, "_d"))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Obj({object.__getattribute__(self, '_d')!r})"


def wrap(value: Any) -> Any:
    """Recursively present dicts as :class:`Obj`, leaving scalars alone."""
    if isinstance(value, dict):
        return Obj(value)
    if isinstance(value, list):
        return [wrap(v) for v in value]
    return value


def parse_timestamp(value: str) -> datetime | str:
    """Parse an Alpaca ISO-8601 timestamp, returning the input if it will not parse."""
    try:
        return datetime.fromisoformat(_NANOS.sub(r"\1", value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return value


def _coerce(key: str, value: Any) -> Any:
    if key in _DATETIME_KEYS and isinstance(value, str) and value:
        return parse_timestamp(value)
    return wrap(value)


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _numeric(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Return a copy with the named string fields coerced to floats."""
    out = dict(payload)
    for key in keys:
        if key in out and isinstance(out[key], str):
            out[key] = _to_float(out[key])
    return out


# ------------------------------------------------------------------ account


def adapt_account(payload: dict[str, Any]) -> Any:
    """`get_account_info` -> an account object with numeric money fields."""
    return wrap(_numeric(payload, _NUMERIC_ACCOUNT_KEYS))


def adapt_clock(payload: dict[str, Any]) -> Any:
    """`get_clock` -> a clock object with real datetimes."""
    return wrap(payload)


def adapt_positions(payload: Any) -> list[Any]:
    """`get_all_positions` -> a list of position objects with numeric fields."""
    rows = payload if isinstance(payload, list) else (payload or {}).get("positions") or []
    return [wrap(_numeric(row, _NUMERIC_POSITION_KEYS)) for row in rows]


def option_positions(positions: list[Any]) -> list[Any]:
    """Positions that are option contracts rather than shares.

    Alpaca marks these ``us_option``; the symbol-length check is a fallback in
    case the field's representation shifts.
    """
    out = []
    for pos in positions:
        asset_class = str(getattr(pos, "asset_class", "") or "").lower()
        symbol = str(getattr(pos, "symbol", "") or "")
        if "option" in asset_class or len(symbol) > 10:
            out.append(pos)
    return out


# ------------------------------------------------------------ market data


def adapt_spot(payload: dict[str, Any]) -> float:
    """`get_stock_latest_trade` -> the last traded price."""
    trades = (payload or {}).get("trades") or {}
    if trades:
        first = next(iter(trades.values()))
        return _to_float(first.get("p"))
    trade = (payload or {}).get("trade") or {}
    return _to_float(trade.get("p"))


def adapt_bars(payload: dict[str, Any]):
    """`get_stock_bars` -> a DataFrame with the long column names.

    The IV module's realized-volatility proxy reads ``close``/``high``/``low``.
    """
    import pandas as pd

    rows = (payload or {}).get("bars") or []
    if isinstance(rows, dict):  # keyed by symbol when several were requested
        rows = next(iter(rows.values()), []) if rows else []
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"])
    return pd.DataFrame(rows).rename(columns={
        "o": "open", "h": "high", "l": "low", "c": "close",
        "v": "volume", "n": "trade_count", "vw": "vwap", "t": "timestamp",
    })


def adapt_chain(payload: dict[str, Any]) -> dict[str, Any]:
    """`get_option_chain` / `get_option_snapshot` -> ``{contract_symbol: snapshot}``.

    Contracts with no ``greeks`` are kept: the caller decides whether a given
    use needs them, and `options_calculator` already counts them under its
    ``no_greeks`` rejection tally rather than treating them as an error.
    """
    snapshots = (payload or {}).get("snapshots") or {}
    return {symbol: wrap(snap) for symbol, snap in snapshots.items()}


def chain_has_greeks(chain: dict[str, Any]) -> int:
    return sum(1 for snap in chain.values() if getattr(snap, "greeks", None) is not None)


# ------------------------------------------------- chain request bracketing


def strike_bracket(spot: float, pct: float) -> tuple[float, float]:
    """The strike window to request around spot.

    Deliberately generous. ``get_option_chain`` truncates a strike-ascending
    ordering, so an unbracketed request spends its whole limit on worthless
    deep-OTM strikes — but a bracket tuned to today's IV returns nothing in a
    quieter week, because the -0.15..-0.20 delta window moves with IV and DTE.
    """
    return (round(spot * (1.0 - pct), 2), round(spot * (1.0 + pct), 2))


class BracketDiagnosis:
    """Why a chain fetch produced no tradeable candidates.

    Two very different failures look identical from the outside, and the fix
    for one is useless for the other:

    ``no_candidates_in_delta_window``
        The bracket was wide enough — it contained strikes on both sides of the
        target window — but nothing landed inside it. A legitimate market
        condition; the model should decline.

    ``bracket_too_narrow``
        A defect. The returned strikes never straddled the window, so the
        request itself was too tight or was truncated by ``limit``. Widening
        ``chain.strike_bracket_pct`` is the fix.
    """

    NO_CANDIDATES = "no_candidates_in_delta_window"
    TOO_NARROW = "bracket_too_narrow"
    OK = "ok"

    def __init__(self, outcome: str, detail: str, observed: dict[str, Any]):
        self.outcome, self.detail, self.observed = outcome, detail, observed

    @property
    def is_defect(self) -> bool:
        return self.outcome == self.TOO_NARROW

    def to_journal(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "detail": self.detail, **self.observed}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"BracketDiagnosis({self.outcome}: {self.detail})"


def diagnose_bracket(
    chain: dict[str, Any],
    delta_range: tuple[float, float],
    bracket: tuple[float, float],
    limit: int,
    candidates_found: int,
) -> BracketDiagnosis:
    """Decide whether an empty candidate list was the market or the request."""
    delta_low, delta_high = sorted(delta_range)  # e.g. (-0.20, -0.15)

    deltas = [
        float(getattr(snap.greeks, "delta", 0) or 0)
        for snap in chain.values()
        if getattr(snap, "greeks", None) is not None
    ]
    observed: dict[str, Any] = {
        "contracts_returned": len(chain),
        "contracts_with_greeks": len(deltas),
        "bracket": list(bracket),
        "limit": limit,
        "delta_window": [delta_low, delta_high],
        "observed_delta_range": [min(deltas), max(deltas)] if deltas else None,
    }

    if candidates_found > 0:
        return BracketDiagnosis(BracketDiagnosis.OK, "candidates found", observed)

    if not deltas:
        return BracketDiagnosis(
            BracketDiagnosis.TOO_NARROW,
            "No contract in the response carried Greeks at all, which is what an "
            "unbracketed or badly-bracketed request returns — the limit was spent "
            "on deep-OTM strikes Alpaca publishes no Greeks for.",
            observed,
        )

    if len(chain) >= limit:
        return BracketDiagnosis(
            BracketDiagnosis.TOO_NARROW,
            f"The response hit the {limit}-contract limit, so it was truncated and "
            "the target strikes may lie beyond the returned range.",
            observed,
        )

    # Deltas are negative for puts: "further OTM" is closer to zero.
    if min(deltas) > delta_high:
        return BracketDiagnosis(
            BracketDiagnosis.TOO_NARROW,
            f"Every strike returned was further OTM than the target window "
            f"(least-OTM delta {min(deltas):.4f} vs window high {delta_high}). "
            "The bracket did not reach close enough to spot.",
            observed,
        )
    if max(deltas) < delta_low:
        return BracketDiagnosis(
            BracketDiagnosis.TOO_NARROW,
            f"Every strike returned was closer to the money than the target window "
            f"(most-OTM delta {max(deltas):.4f} vs window low {delta_low}). "
            "The bracket did not reach far enough OTM.",
            observed,
        )

    return BracketDiagnosis(
        BracketDiagnosis.NO_CANDIDATES,
        f"The bracket straddled the target window (observed deltas "
        f"{min(deltas):.4f}..{max(deltas):.4f}) but no strike fell inside "
        f"{delta_low}..{delta_high} with an acceptable quote.",
        observed,
    )


# ------------------------------------------------------- request arguments


def chain_request(
    ticker: str,
    spot: float,
    *,
    feed: str,
    bracket_pct: float,
    min_dte: int,
    max_dte: int,
    limit: int,
    today: date | None = None,
) -> dict[str, Any]:
    """Build the `get_option_chain` arguments for one underlying.

    Every field here is load-bearing: without ``feed`` the call 403s, without
    the strike bracket the ``limit`` is spent on worthless strikes, and without
    the expiry window the response spans months of contracts.
    """
    today = today or date.today()
    low, high = strike_bracket(spot, bracket_pct)
    return {
        "underlying_symbol": ticker,
        "type": "put",
        "feed": feed,
        "strike_price_gte": low,
        "strike_price_lte": high,
        "expiration_date_gte": (today + timedelta(days=min_dte)).isoformat(),
        "expiration_date_lte": (today + timedelta(days=max_dte)).isoformat(),
        "limit": limit,
    }


class MCPBrokerView:
    """A synchronous, read-only view over already-fetched MCP payloads.

    `position_manager.build_portfolio_state` is ported code and expects an
    object with `get_account`, `get_option_positions` and `get_option_snapshots`.
    Rather than duplicate its aggregate-Greeks logic in two places — the
    orchestrator and the dashboard both need the same portfolio shape — this
    presents MCP results in the interface it already speaks.

    It performs no I/O: everything is fetched by the caller inside one async MCP
    session and handed over here, which keeps the async boundary at the edge and
    the ported code untouched.
    """

    def __init__(self, account_payload: dict, positions_payload: Any,
                 snapshots_payload: dict | None = None):
        self._account = adapt_account(account_payload or {})
        self._positions = adapt_positions(positions_payload)
        self._snapshots = adapt_chain(snapshots_payload or {})

    def get_account(self) -> Any:
        return self._account

    def get_positions(self) -> list[Any]:
        return self._positions

    def get_option_positions(self) -> list[Any]:
        return option_positions(self._positions)

    def get_option_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        return {s: self._snapshots[s] for s in symbols if s in self._snapshots}


def summarise_chain(
    payload: dict[str, Any],
    *,
    delta_range: tuple[float, float] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    """Compact an option chain into the handful of rows a model can act on.

    A bracketed SPY chain is ~700 contracts and ~300,000 characters of JSON.
    Handing that to a model means it gets truncated, and because the chain is
    ordered by ascending strike the surviving fragment is nothing but worthless
    deep-OTM contracts — the ones Alpaca publishes no Greeks for. The model then
    cannot find a tradeable strike, re-queries, and burns its whole turn budget
    without proposing. That is exactly the `no_proposal_turn_limit` defect.

    So the model is given a table instead of a dump: one row per contract that
    actually carries Greeks, ordered by how close it sits to the target delta
    window, capped at `limit`. Roughly 5KB instead of 300KB, and every row is a
    strike it could legitimately choose.

    Nothing here filters on tradeability or risk — the rows are ordered, not
    judged. The risk gate remains the only thing that can veto a trade.
    """
    snapshots = (payload or {}).get("snapshots") or {}
    if not snapshots:
        return {"contracts": [], "total_contracts": 0, "shown": 0,
                "note": "The chain came back empty for this request."}

    low, high = sorted(delta_range) if delta_range else (-0.20, -0.15)
    midpoint = (low + high) / 2.0

    rows: list[dict[str, Any]] = []
    without_greeks = 0

    for symbol, snap in snapshots.items():
        greeks = snap.get("greeks") if isinstance(snap, dict) else None
        if not greeks:
            without_greeks += 1
            continue
        quote = (snap.get("latestQuote") or {}) if isinstance(snap, dict) else {}
        delta = greeks.get("delta")
        if delta is None:
            without_greeks += 1
            continue
        rows.append({
            "symbol": symbol,
            "strike": strike_from_symbol(symbol),
            "expiry": str(expiry_from_symbol(symbol) or ""),
            "delta": delta,
            "iv": snap.get("impliedVolatility"),
            "bid": quote.get("bp"),
            "ask": quote.get("ap"),
        })

    # Closest to the middle of the target window first, so the strikes the
    # strategy actually wants survive the cap.
    rows.sort(key=lambda r: abs(float(r["delta"]) - midpoint))
    shown = rows[:limit]
    # Present them in a human order once the useful ones have been selected.
    shown.sort(key=lambda r: (r["expiry"], -(r["strike"] or 0)))

    in_window = sum(1 for r in shown if low <= float(r["delta"]) <= high)

    return {
        "contracts": shown,
        "total_contracts": len(snapshots),
        "with_greeks": len(rows),
        "without_greeks": without_greeks,
        "shown": len(shown),
        "target_delta_window": [low, high],
        "in_target_window": in_window,
        "note": (
            f"Compacted from {len(snapshots)} contracts to the {len(shown)} nearest "
            f"the {low} to {high} delta window. {in_window} of them sit inside it. "
            "Strikes are in dollars; delta is negative for puts."
        ),
    }


def occ_symbol(root: str, expiry: str | date, strike: float, kind: str = "P") -> str:
    """Build an OCC contract symbol, e.g. ``SPY260831P00753000``.

    Format is ROOT + YYMMDD + C/P + an 8-digit strike in thousandths. The
    inverse pair lives in `agent.iv` (`strike_from_symbol`, `expiry_from_symbol`)
    because the ported IV module already needed it.
    """
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry)
    return f"{root.upper()}{expiry:%y%m%d}{kind}{int(round(strike * 1000)):08d}"


def bars_request(
    ticker: str,
    *,
    feed: str = "iex",
    lookback_days: int = 400,
    today: date | None = None,
) -> dict[str, Any]:
    """Build `get_stock_bars` arguments for the realized-volatility proxy.

    ``start`` is not optional in practice. The schema says omitting it uses a
    "relative lookback", but that lookback returns only a handful of recent
    bars — four, when observed — and IV rank's `rv_proxy` fallback needs a year
    of closes to rank against. Without it, IV rank silently reports
    ``unavailable`` rather than failing, which is the worst kind of quiet.

    The default reaches past 252 trading days so a full year of calendar data is
    available even across holidays.
    """
    today = today or date.today()
    return {
        "symbols": ticker,
        "timeframe": "1Day",
        "feed": feed,
        "start": (today - timedelta(days=lookback_days)).isoformat(),
        "limit": 10000,
    }


def order_request(
    *,
    sell_symbol: str,
    buy_symbol: str,
    contracts: int,
    limit_price: float,
    client_order_id: str,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Build `place_option_order` arguments for a two-leg credit spread.

    Two details are pinned by `tests/test_order_sign.py`:

    - **A credit is a negative limit price.** Confirmed from the live tool
      schema: "positive = debit/cost, negative = credit/proceeds". Inverting it
      submits an order willing to *pay* to open a position that should collect.
    - **Every scalar is a string.** ``qty``, ``limit_price`` and each leg's
      ``ratio_qty`` are typed ``string`` in the schema; passing numbers is a
      validation error at the worst possible moment.
    """
    if limit_price >= 0:
        raise ValueError(
            f"A credit spread must submit a NEGATIVE limit price; got {limit_price}. "
            "Positive is a debit — this order would pay to open a position that "
            "should collect."
        )
    return {
        "qty": str(int(contracts)),
        "order_class": "mleg",
        "type": "limit",
        "time_in_force": time_in_force,
        "limit_price": str(round(limit_price, 2)),
        "client_order_id": client_order_id,
        "legs": [
            {"symbol": sell_symbol, "ratio_qty": "1", "side": "sell",
             "position_intent": "sell_to_open"},
            {"symbol": buy_symbol, "ratio_qty": "1", "side": "buy",
             "position_intent": "buy_to_open"},
        ],
    }
