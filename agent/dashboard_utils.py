"""Pure helpers for the dashboard.

Kept out of ``streamlit_app.py`` so they can be unit-tested without starting a
Streamlit runtime. Everything here is a plain function over plain data: no
Streamlit imports, no I/O, no network.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

PNL_COLUMNS = ["Closed at", "Trade P&L", "Cumulative P&L", "Ticker"]


def money(value: Any, decimals: int = 2) -> str:
    """Format a dollar amount, with the sign outside the currency symbol.

    ``-$212.00`` rather than ``$-212.00`` — the latter reads as a typo at a
    glance, and losses are exactly the numbers a reader needs to parse fastest.
    """
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{decimals}f}"


def format_countdown(delta) -> str:
    """Render a timedelta as a compact countdown, e.g. ``1d 20h 14m``.

    Drops to minutes-and-seconds under an hour so the last stretch before the
    opening bell is actually readable, and clamps to ``now`` rather than
    printing a negative countdown when the boundary has just passed.
    """
    total = int(delta.total_seconds())
    if total <= 0:
        return "now"

    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def is_rejection(event: dict[str, Any]) -> bool:
    """Whether an event represents a trade the risk gate blocked.

    The journal writes rejections as ``event_type: "trade_rejected"``. Some
    external tooling describes them instead as ``status: "rejected"``, so both
    shapes are accepted — a rejection missed because of a field-name mismatch
    would silently hide exactly the evidence this panel exists to show.
    """
    return (
        event.get("event_type") == "trade_rejected"
        or str(event.get("status", "")).lower() == "rejected"
    )


def rejection_reason(event: dict[str, Any]) -> str:
    """The human-readable reason a trade was blocked, from either schema."""
    return event.get("reason") or event.get("rejection_reason") or "No reason recorded."


def build_pnl_series(events: list[dict[str, Any]]) -> pd.DataFrame:
    """Cumulative realized P&L over time, one point per closed position.

    Realized only. Marking open positions to market would make the line jump
    around on quote noise and would overstate performance for a premium-selling
    strategy, where an open spread looks like a winner right up until it isn't.

    Events are sorted by timestamp before accumulating, because the journal is
    append-ordered by *write* time and a backfilled or out-of-order entry would
    otherwise produce a curve that walks backwards.
    """
    closes = [
        event for event in events
        if event.get("event_type") == "position_exit"
        and (event.get("execution") or {}).get("realized_pnl") is not None
    ]
    if not closes:
        return pd.DataFrame(columns=PNL_COLUMNS)

    closes.sort(key=lambda e: e.get("timestamp", ""))

    rows = []
    running = 0.0
    for event in closes:
        pnl = float((event.get("execution") or {})["realized_pnl"])
        running += pnl
        rows.append({
            "Closed at": pd.to_datetime(event.get("timestamp"), errors="coerce", utc=True),
            "Trade P&L": round(pnl, 2),
            "Cumulative P&L": round(running, 2),
            "Ticker": event.get("ticker", ""),
        })

    frame = pd.DataFrame(rows).dropna(subset=["Closed at"])
    return frame if not frame.empty else pd.DataFrame(columns=PNL_COLUMNS)
