"""Portfolio state, aggregate Greeks, and exit decisions.

Runs at the start of every cycle, before the risk gate, because the gate's
inputs live here: net delta-dollars for Rule 3, daily P&L for the kill-switch
in Rule 8, buying power for Rule 9, and the open-position list for the
duplicate check in Rule 5. A stale portfolio snapshot means a gate decision made
on stale facts, so this is refreshed from the broker every cycle rather than
cached.

It also decides when to close: take profit at half the credit, cut at a loss
equal to the credit received, and always be flat by two days to expiry. Credit
spreads cannot simply be held to expiration — a short strike that finishes near
the money carries assignment and pin risk that the defined-risk structure does
not protect against.
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

import logging
from datetime import date, datetime
from typing import Any

from .config import AgentConfig
from .iv import expiry_from_symbol, strike_from_symbol

logger = logging.getLogger(__name__)


def parse_position(position: Any, as_of: date | None = None) -> dict[str, Any]:
    """Normalise an Alpaca position object into a plain dict.

    Alpaca returns option positions with the OCC contract symbol, so the
    underlying, expiry and strike are parsed back out of it.
    """
    as_of = as_of or datetime.now().date()
    symbol = str(position.symbol)
    expiry = expiry_from_symbol(symbol)
    strike = strike_from_symbol(symbol)

    # OCC root is everything before the 15-character date/type/strike tail.
    underlying = symbol[:-15].strip() if len(symbol) > 15 else symbol

    qty = float(getattr(position, "qty", 0) or 0)
    return {
        "symbol": symbol,
        "underlying": underlying,
        "expiry": expiry.isoformat() if expiry else None,
        "dte": (expiry - as_of).days if expiry else None,
        "strike": strike,
        "contracts": qty,
        "side": "short" if qty < 0 else "long",
        "avg_entry_price": float(getattr(position, "avg_entry_price", 0) or 0),
        "market_value": float(getattr(position, "market_value", 0) or 0),
        "cost_basis": float(getattr(position, "cost_basis", 0) or 0),
        "unrealized_pl": float(getattr(position, "unrealized_pl", 0) or 0),
        "unrealized_plpc": float(getattr(position, "unrealized_plpc", 0) or 0),
        "current_price": float(getattr(position, "current_price", 0) or 0),
    }


def build_portfolio_state(broker, config: AgentConfig, spot_prices: dict[str, float] | None = None) -> dict[str, Any]:
    """Snapshot the account and open book, with aggregate Greeks.

    Greeks come from a live snapshot request for the held contracts, since a
    position's delta today is not the delta it was opened at.
    """
    spot_prices = spot_prices or {}
    account = broker.get_account()

    nav = float(account.equity or 0)
    last_equity = float(getattr(account, "last_equity", 0) or nav)
    buying_power = float(account.buying_power or 0)

    daily_pnl = nav - last_equity
    daily_pnl_pct = (daily_pnl / last_equity) if last_equity else 0.0

    raw_positions = broker.get_option_positions()
    positions = [parse_position(p) for p in raw_positions]

    # Live Greeks for the open contracts.
    greeks_by_symbol: dict[str, Any] = {}
    if positions:
        try:
            snapshots = broker.get_option_snapshots([p["symbol"] for p in positions])
            greeks_by_symbol = dict(snapshots)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not refresh position Greeks: %s", exc)

    net_delta_dollars = 0.0
    total_theta = 0.0
    total_vega = 0.0
    total_gamma = 0.0

    for position in positions:
        snapshot = greeks_by_symbol.get(position["symbol"])
        greeks = getattr(snapshot, "greeks", None) if snapshot else None
        delta = float(getattr(greeks, "delta", 0) or 0) if greeks else 0.0
        theta = float(getattr(greeks, "theta", 0) or 0) if greeks else 0.0
        vega = float(getattr(greeks, "vega", 0) or 0) if greeks else 0.0
        gamma = float(getattr(greeks, "gamma", 0) or 0) if greeks else 0.0

        position["delta"] = round(delta, 4)
        position["theta"] = round(theta, 4)
        position["vega"] = round(vega, 4)
        position["gamma"] = round(gamma, 6)
        position["iv"] = getattr(snapshot, "implied_volatility", None) if snapshot else None

        # Signed by contract count: a short leg contributes the opposite sign.
        underlying_spot = spot_prices.get(position["underlying"], 0.0)
        net_delta_dollars += delta * 100.0 * position["contracts"] * underlying_spot
        total_theta += theta * 100.0 * position["contracts"]
        total_vega += vega * 100.0 * position["contracts"]
        total_gamma += gamma * 100.0 * position["contracts"]

    return {
        "nav": nav,
        "last_equity": last_equity,
        "cash": float(account.cash or 0),
        "buying_power": buying_power,
        "starting_buying_power": _starting_buying_power(config, buying_power),
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 6),
        "open_positions": positions,
        "position_count": len(positions),
        "net_delta_dollars": round(net_delta_dollars, 2),
        "portfolio_theta": round(total_theta, 2),
        "portfolio_vega": round(total_vega, 2),
        "portfolio_gamma": round(total_gamma, 4),
        "unrealized_pnl": round(sum(p["unrealized_pl"] for p in positions), 2),
        "snapshot_at": datetime.now().astimezone().isoformat(),
    }


def _starting_buying_power(config: AgentConfig, current_bp: float) -> float:
    """The Rule 9 baseline, persisted on first run.

    Anchoring the reserve to a fixed baseline rather than to today's buying
    power matters: if the book draws down, current buying power falls with it,
    and a reserve measured against the shrinking number would keep permitting
    trades exactly when it should stop.
    """
    import json

    configured = config.options.get("account", {}).get("starting_buying_power")
    if configured:
        return float(configured)

    path = config.paths.get("baseline")
    if path and path.exists():
        try:
            with open(path, encoding="utf-8") as handle:
                return float(json.load(handle)["starting_buying_power"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass

    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "starting_buying_power": current_bp,
                        "recorded_at": datetime.now().astimezone().isoformat(),
                    },
                    handle,
                    indent=2,
                )
        except OSError as exc:
            logger.warning("Could not persist buying-power baseline: %s", exc)
    return current_bp


def group_into_spreads(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group individual option legs back into the spreads they belong to.

    Alpaca reports each leg of a multi-leg position as its own position, so a
    bull put spread comes back as two rows. Evaluating exits per row would be
    wrong in a specific and dangerous way: the short leg decays into profit
    while the long leg decays into loss, so a profit target would close the
    short leg alone and leave an orphaned long put behind.

    Legs are grouped by (underlying, expiry), which is exactly what defines a
    vertical spread. P&L is then judged on the group as a whole.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for position in positions:
        key = (position.get("underlying", ""), str(position.get("expiry")))
        group = groups.setdefault(key, {
            "underlying": position.get("underlying"),
            "expiry": position.get("expiry"),
            "dte": position.get("dte"),
            "legs": [],
            "symbols": [],
            "cost_basis": 0.0,
            "unrealized_pl": 0.0,
            "market_value": 0.0,
            "strikes": [],
        })
        group["legs"].append(position)
        group["symbols"].append(position["symbol"])
        group["cost_basis"] += position.get("cost_basis") or 0.0
        group["unrealized_pl"] += position.get("unrealized_pl") or 0.0
        group["market_value"] += position.get("market_value") or 0.0
        if position.get("strike") is not None:
            group["strikes"].append(position["strike"])

    for group in groups.values():
        group["strikes"].sort(reverse=True)
        group["leg_count"] = len(group["legs"])
        group["label"] = (
            f"{group['underlying']} {group['expiry']} "
            + "/".join(f"{s:g}" for s in group["strikes"])
        )
    return list(groups.values())


def decide_exits(portfolio: dict[str, Any], config: AgentConfig) -> list[dict[str, Any]]:
    """Which open spreads should be closed this cycle, and why.

    Operates on whole spreads, never on individual legs — see
    ``group_into_spreads`` for why that distinction matters.

    Three triggers, checked in order of urgency:

    1. **DTE floor** — close at 2 days regardless of P&L. Gamma and pin risk in
       the final days are not compensated by the remaining theta.
    2. **Profit target** — 50% of the credit captured. Holding for the last half
       means risking the whole spread to earn what is left, which is a
       progressively worse bet as expiry approaches.
    3. **Stop loss** — down by the credit received. Cuts the tail before the
       spread goes to max loss.
    """
    pm = config.position_management
    profit_target = float(pm.get("exit_if_profit_pct", 50)) / 100.0
    loss_limit = float(pm.get("exit_if_loss_pct", -100)) / 100.0
    dte_floor = int(pm.get("exit_if_dte_below", 2))

    exits: list[dict[str, Any]] = []
    for group in group_into_spreads(portfolio.get("open_positions", [])):
        dte = group.get("dte")
        if dte is not None and dte <= dte_floor:
            exits.append({**group, "exit_reason": (
                f"DTE {dte} at or below the {dte_floor}-day floor. Closing "
                f"{group['label']} before gamma and pin risk take over."
            )})
            continue

        # For a net-credit spread the combined cost basis is negative: cash was
        # received to open. Its magnitude is the credit the exit is judged
        # against.
        credit = abs(group.get("cost_basis") or 0.0)
        if credit <= 0:
            continue

        captured = (group.get("unrealized_pl") or 0.0) / credit

        if captured >= profit_target:
            exits.append({**group, "exit_reason": (
                f"Captured {captured:.0%} of the credit on {group['label']}, at or past "
                f"the {profit_target:.0%} profit target."
            )})
        elif captured <= loss_limit:
            exits.append({**group, "exit_reason": (
                f"{group['label']} is down {captured:.0%} against a {loss_limit:.0%} stop. "
                "Cutting before the spread reaches max loss."
            )})

    return exits
