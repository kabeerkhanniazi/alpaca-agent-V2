"""Live dashboard for the options agent.

Read-only by construction. Nothing on this page can place, cancel or stop a
trade — the mockup's "EXECUTE LIVE" and "KILL SWITCH" controls are rendered as
status pills, not buttons, because this is deployed publicly and a stranger's
click must not reach the broker. The only interactive controls are the refresh
button, the journal tabs, and the chart.

Data comes from two places: Alpaca for account, positions and chain state, and
the trade journal for history. Either can be missing — a deployment without
credentials still renders every journal-derived panel rather than a wall of
errors. See agent/journal_source.py for how the journal is resolved.

Visual language is ported from docs/design/alpaca.html. The stylesheet lives in
agent/dashboard_theme.py and is injected once, below.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import dashboard_theme as T  # noqa: E402
from agent.config import ConfigError, load_config  # noqa: E402
from agent.dashboard_utils import (  # noqa: E402
    build_pnl_series,
    format_countdown,
    is_rejection,
    money,
    rejection_reason,
)
from agent.journal_source import (  # noqa: E402
    SOURCE_LOCAL,
    SOURCE_NONE,
    SOURCE_REMOTE,
    SOURCE_SNAPSHOT,
    load_journal,
)
from agent.journal import TradeJournal  # noqa: E402

st.set_page_config(
    page_title="ATA",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(T.CSS, unsafe_allow_html=True)


def html(markup: str) -> None:
    """Shorthand for injecting a rendered component."""
    st.markdown(markup, unsafe_allow_html=True)


# --------------------------------------------------------------- data

@st.cache_resource
def get_config():
    return load_config(with_credentials=False)


@st.cache_data(ttl=60)
def get_journal_payload():
    """Journal events, from the freshest source available."""
    config = get_config()
    return load_journal(local_path=config.paths["journal"])


@st.cache_data(ttl=30)
def get_live_state():
    """Everything the live panels need, from ONE MCP session.

    The MCP server is a subprocess, so opening a session per panel would spawn
    three of them per refresh. This gathers account, positions, clock, spot and
    IV rank in a single connection instead.

    Credentials are resolved here rather than at import time, so a deployment
    without them still renders: every live panel reports itself unavailable and
    every journal-derived panel works exactly as normal.
    """
    import asyncio

    from agent.adapters import (
        MCPBrokerView, adapt_bars, adapt_chain, adapt_clock, adapt_positions,
        adapt_spot, bars_request, chain_request, option_positions,
    )
    from agent.iv import atm_implied_volatility, compute_iv_rank
    from agent.mcp_client import MCPClient
    from agent.position_manager import build_portfolio_state

    config = get_config()

    async def fetch():
        async with MCPClient() as mcp:
            clock = adapt_clock(await mcp.call_read("get_clock", {}))
            account = await mcp.call_read("get_account_info", {})
            positions = await mcp.call_read("get_all_positions", {})

            market = []
            for ticker in config.underlyings:
                try:
                    spot = adapt_spot(await mcp.call_read(
                        "get_stock_latest_trade", {"symbols": ticker, "feed": "iex"}))
                    chain = adapt_chain(await mcp.call_read("get_option_chain", chain_request(
                        ticker, spot, feed=config.options_feed,
                        bracket_pct=config.chain["strike_bracket_pct"],
                        min_dte=config.min_dte, max_dte=config.max_dte,
                        limit=config.chain["limit"])))
                    bars = adapt_bars(await mcp.call_read(
                        "get_stock_bars", bars_request(ticker)))
                    info = compute_iv_rank(
                        ticker, atm_implied_volatility(chain, spot),
                        list(bars["close"]), config.paths["iv_history"], config.iv,
                    )
                    market.append({"ticker": ticker, "spot": spot, **info})
                except Exception as exc:  # noqa: BLE001 — one bad ticker must not blank the page
                    market.append({"ticker": ticker, "spot": None, "atm_iv": None,
                                   "iv_rank": None, "regime": "UNKNOWN",
                                   "iv_rank_source": str(exc)[:40]})

            # Live Greeks for held contracts, so the portfolio panel shows
            # today's delta rather than the delta each position opened at.
            held = [str(p.symbol) for p in option_positions(adapt_positions(positions))]
            snapshots = {}
            if held:
                try:
                    snapshots = await mcp.call_read("get_option_snapshot", {
                        "symbols": ",".join(held), "feed": config.options_feed})
                except Exception:  # noqa: BLE001 — Greeks are a nicety, not the page
                    snapshots = {}

            spots = {row["ticker"]: row["spot"] for row in market}
            view = MCPBrokerView(account, positions, snapshots)
            portfolio = build_portfolio_state(
                view, config, {k: v for k, v in spots.items() if v})

            return {
                "clock": {
                    "is_open": bool(clock.is_open),
                    "timestamp": clock.timestamp,
                    "next_open": getattr(clock, "next_open", None),
                    "next_close": getattr(clock, "next_close", None),
                },
                "portfolio": portfolio,
                "spots": spots,
                "market": market,
            }

    try:
        return asyncio.run(fetch())
    except Exception as exc:  # noqa: BLE001 — see below
        # A deployment without credentials, or an Alpaca outage, must still
        # render: the live panels report themselves unavailable and every
        # journal-derived panel works exactly as normal. Returning a degraded
        # payload makes that a deliberate state rather than an incidental one.
        #
        # This also absorbs the anyio "cancel scope in a different task"
        # teardown error, which surfaces when the MCP subprocess is closed from
        # Streamlit's script thread rather than the loop that opened it.
        return {
            "clock": None,
            "portfolio": None,
            "spots": {},
            "market": [],
            "unavailable": str(exc)[:200],
        }


def _live_or_raise() -> dict:
    """The live state, or a clear error for `safe()` to turn into a notice.

    Raising here rather than returning None keeps the existing contract: every
    caller goes through `safe()`, which renders the "Live data unavailable" card
    with the reason attached.
    """
    state = get_live_state()
    if state.get("unavailable"):
        raise RuntimeError(state["unavailable"])
    return state


def get_account_snapshot():
    state = _live_or_raise()
    return state["portfolio"], state["spots"]


def get_market_state():
    return _live_or_raise()["market"]


def get_clock_state():
    return _live_or_raise()["clock"]


def safe(fn, *args, **kwargs):
    """Call a live-data fetcher, returning (value, error) instead of raising."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def unavailable(message: str) -> str:
    """A neutral placeholder for a panel whose live data could not be fetched."""
    return (
        f'<div class="oa-card oa-accent-neutral">'
        f'<div class="oa-card-head"><span class="oa-label">Live data unavailable</span></div>'
        f'<p class="oa-sub" style="margin-top:2px">{T.esc(message)}</p></div>'
    )


# --------------------------------------------------------------- config

try:
    config = get_config()
except ConfigError as exc:
    st.error(f"Configuration problem: {exc}")
    st.stop()

payload = get_journal_payload()
events_all = list(reversed(payload.events))          # newest first
journal = TradeJournal(config.paths["journal"])
stats = TradeJournal.stats_from_events(payload.events)

portfolio_result, portfolio_error = safe(get_account_snapshot)
portfolio, spots = portfolio_result if portfolio_result else (None, {})
clock_state, clock_error = safe(get_clock_state)

# ------------------------------------------------------------- header

# The mockup's top nav (Mainframe / Nodes / Liquidity / Risk) pointed nowhere.
# In-page anchor jumps would be the honest replacement, but confirming they
# actually scroll inside Streamlit's container needs a headless browser that is
# not available here — and an unverified nav is exactly the dead link the design
# brief rules out. The nav is dropped; the section ids below remain, so a shared
# URL ending in #risk-gate still lands in the right place.
html(
    f'<div style="margin-bottom:14px">'
    f'<h1 style="font-family:var(--font-display);font-weight:800;font-size:1.6rem;'
    f'letter-spacing:0.06em;text-transform:uppercase;color:var(--secondary);'
    f'margin:0" class="oa-glow-secondary">Alpaca Trading Agent</h1>'
    f'<p class="oa-sub" style="margin-top:4px">Defined-risk credit spreads on '
    f'{T.esc(", ".join(config.underlyings))}. Every position passes a nine-rule '
    f'deterministic risk gate — no language model in the decision path.</p></div>'
)

# ------------------------------------------------------- market banner

if clock_state:
    now = clock_state["timestamp"]
    is_open = clock_state["is_open"]
    target = clock_state["next_close"] if is_open else clock_state["next_open"]
    label = "Closes in" if is_open else "Opens in"

    units: list[tuple[str, str]] = []
    if target:
        parts = format_countdown(target - now).split()
        units = [(p[:-1], p[-1]) for p in parts if p != "now"]
        session = (
            f"Session ends {target:%H:%M %Z on %a %d %b}. "
            # Read from config rather than written here: the cadence has changed
            # once already, and a hardcoded figure quietly becomes a lie.
            f"The agent runs a cycle every {config.agent.get('cycle_interval_seconds', 900) // 60} "
            "minutes until then."
            if is_open else
            f"Next session {target:%H:%M %Z on %a %d %b}. "
            "Scheduled cycles no-op until the bell."
        )
    else:
        session = "Session boundary unknown."
    html(T.market_banner(is_open, units, label, session))
else:
    html(
        '<div class="oa-banner"><div class="oa-banner-left">'
        '<div class="oa-orb">' + T.icon("block", size=26) + '</div><div>'
        '<h2 class="oa-banner-title t-variant">MARKET STATUS UNAVAILABLE</h2>'
        f'<p class="oa-banner-sub">{T.esc(str(clock_error)[:150])}</p>'
        '</div></div></div>'
    )

# ------------------------------------------------------------ sidebar

with st.sidebar:
    html(
        '<div style="display:flex;align-items:center;gap:11px;margin-bottom:18px">'
        '<div style="width:38px;height:38px;border-radius:9px;display:flex;'
        'align-items:center;justify-content:center;background:var(--surface-container-high);'
        'border:1px solid rgba(255,45,120,0.35)">' + T.icon("shield", "#ff2d78", 19) + '</div>'
        '<div><div style="font-family:var(--font-display);font-weight:800;'
        'font-size:1rem;color:var(--primary);letter-spacing:-0.01em">ALPACA AGENT</div>'
        '<div class="oa-label" style="font-size:0.6rem">Read-only console</div></div></div>'
    )

    # Run mode, from the most recent cycle the agent actually logged.
    cycles = [e for e in payload.events if e.get("event_type") == "cycle_summary"]
    if cycles:
        mode = str(cycles[-1].get("mode", "UNKNOWN")).upper().replace(" ", "-")
        mode_tone = "primary" if mode == "LIVE" else "neutral"
    else:
        mode, mode_tone = "UNKNOWN", "neutral"

    if portfolio:
        drawdown = -min(0.0, portfolio["daily_pnl_pct"])
        tripped = drawdown > config.kill_switch_pct
        ks_text = (
            f"KILL-SWITCH: {'TRIPPED' if tripped else 'ARMED'} "
            f"· {drawdown:.2%} / {config.kill_switch_pct:.0%}"
        )
        ks_tone = "error" if tripped else "secondary"
    else:
        ks_text, ks_tone = f"KILL-SWITCH: ARMED · {config.kill_switch_pct:.0%} limit", "neutral"

    # Status only — spans, not buttons. Nothing here can reach the broker.
    html(T.pill_row([T.status_pill(f"MODE: {mode}", mode_tone),
                     T.status_pill(ks_text, ks_tone)]))

    rows = [
        ("Underlyings", ", ".join(config.underlyings)),
        ("Delta window", f"{config.delta_range[0]} to {config.delta_range[1]}"),
        ("DTE window", f"{config.min_dte}–{config.max_dte} days"),
        ("Max risk / trade", f"{config.max_loss_pct:.0%} of NAV"),
        ("Portfolio delta cap", f"{config.max_portfolio_delta_pct:.0%} of NAV"),
        ("Min credit", money(config.min_credit_usd)),
    ]
    html(
        '<div class="oa-glass" style="padding:14px 16px">'
        + "".join(
            f'<div style="margin-bottom:11px"><div class="oa-label">{T.esc(k)}</div>'
            f'<div style="font-family:var(--font-display);font-weight:600;'
            f'font-size:0.92rem;color:var(--on-surface);overflow-wrap:anywhere">{T.esc(v)}</div></div>'
            for k, v in rows
        )
        + '<div style="border-top:1px solid var(--outline-variant);padding-top:11px">'
        '<div class="oa-label t-error">Kill-switch</div>'
        f'<div style="font-family:var(--font-display);font-weight:800;font-size:1.25rem;'
        f'color:var(--error)">−{config.kill_switch_pct:.0%} daily</div></div></div>'
    )

    source_tone = {
        SOURCE_LOCAL: "secondary", SOURCE_REMOTE: "secondary",
        SOURCE_SNAPSHOT: "tertiary", SOURCE_NONE: "neutral",
    }[payload.source]
    html(T.pill_row([T.status_pill(f"JOURNAL: {payload.source} · {payload.age_label}", source_tone)]))
    st.caption(payload.detail)

    if st.button("Refresh now", width="stretch"):
        st.cache_data.clear()
        st.rerun()

# ---------------------------------------------------------- portfolio

html(T.section_heading("Portfolio", "portfolio"))

if portfolio:
    pnl_accent, pnl_class = T.pnl_classes(portfolio["daily_pnl"])
    unreal_accent, unreal_class = T.pnl_classes(portfolio["unrealized_pnl"])
    html(T.card_grid([
        T.metric_card("Net liquidation", money(portfolio["nav"]), "primary",
                      "t-primary", icon_name="bank", glow=True),
        T.metric_card("Daily P&L", money(portfolio["daily_pnl"]), pnl_accent, pnl_class,
                      sub=f"{portfolio['daily_pnl_pct']:+.2%}", icon_name="trend"),
        T.metric_card("Unrealized", money(portfolio["unrealized_pnl"]), unreal_accent,
                      unreal_class, icon_name="trend"),
        T.metric_card("Open positions", str(portfolio["position_count"]), "tertiary",
                      "t-tertiary", icon_name="box"),
        T.metric_card("Buying power", money(portfolio["buying_power"], 0), "secondary",
                      "t-on-surface", icon_name="cash"),
    ]))

    delta_pct = portfolio["net_delta_dollars"] / portfolio["nav"] if portfolio["nav"] else 0
    html(T.card_grid([
        T.metric_card("Net delta", money(portfolio["net_delta_dollars"], 0), "neutral",
                      "t-on-surface", sub=f"{delta_pct:.1%} of NAV", icon_name="gauge"),
        T.metric_card("Theta / day", money(portfolio["portfolio_theta"]), "secondary",
                      "t-secondary", icon_name="pulse"),
        T.metric_card("Vega", money(portfolio["portfolio_vega"]), "neutral",
                      "t-on-surface", icon_name="pulse"),
        T.metric_card("Gamma", f"{portfolio['portfolio_gamma']:.4f}", "neutral",
                      "t-on-surface", icon_name="pulse"),
    ]))

    delta_limit = portfolio["nav"] * config.max_portfolio_delta_pct
    used = abs(portfolio["net_delta_dollars"])
    loss_used = -min(0.0, portfolio["daily_pnl_pct"])
    html(
        '<div class="oa-glass" style="padding:16px 18px;margin-top:6px">'
        '<div class="oa-label" style="margin-bottom:12px">Headroom against the gate\'s '
        'portfolio limits</div>'
        + T.headroom_bar("Portfolio delta · Rule 3", used,
                         f"{money(used, 0)} of {money(delta_limit, 0)}",
                         used / delta_limit if delta_limit else 0.0)
        + T.headroom_bar("Daily drawdown · Rule 8", loss_used,
                         f"{loss_used:.2%} of {config.kill_switch_pct:.0%}",
                         loss_used / config.kill_switch_pct if config.kill_switch_pct else 0.0)
        + "</div>"
    )

    if portfolio["open_positions"]:
        rows = [[
            f'<span class="oa-ticker">{T.esc(p["underlying"])}</span>',
            T.esc(p["symbol"]), f'{p["strike"]:g}', T.esc(p["expiry"]),
            str(p["dte"]), f'{p["contracts"]:g}', f'{p.get("delta", 0):.3f}',
            f'{p.get("theta", 0):.3f}', money(p["avg_entry_price"]),
            money(p["current_price"]),
            f'<span class="{T.pnl_classes(p["unrealized_pl"])[1]}">'
            f'{money(p["unrealized_pl"])}</span>',
        ] for p in portfolio["open_positions"]]
        html(T.table(
            ["Underlying", "Contract", "Strike", "Expiry", "DTE", "Qty",
             "Delta", "Theta", "Entry", "Mark", "Unrealized"],
            rows, "Open positions",
        ))
    else:
        st.info("No open positions. The agent opens at most one new spread per cycle.")
else:
    html(unavailable(
        f"Alpaca account state could not be fetched — {str(portfolio_error)[:180]}. "
        "Journal-derived panels below are unaffected."
    ))

# ------------------------------------------------------ agent reasoning

# This panel *is* the evidence that an AI agent exists. Everything else on this
# dashboard could have been produced by a script; only this shows a model
# reasoning over live data and a deterministic gate ruling on what it proposed.

html(T.section_heading("Agent Reasoning", "agent-reasoning"))

AGENT_EVENTS = (
    "agent_proposal", "no_proposal_declined", "no_proposal_turn_limit",
    "malformed_proposal", "invalid_proposal",
)

# Two healthy outcomes and three defects. Rendering them the same way would hide
# the difference at exactly the moment it matters, so each carries its own tone.
OUTCOME_STYLE = {
    "agent_proposal": ("PROPOSED", "ok"),
    "no_proposal_declined": ("DECLINED", "neutral"),
    "no_proposal_turn_limit": ("TURN LIMIT", "warn"),
    "malformed_proposal": ("MALFORMED", "bad"),
    "invalid_proposal": ("INVALID LEGS", "bad"),
}

OUTCOME_NOTE = {
    "no_proposal_declined":
        "The model judged conditions poor and declined. This is the strategy "
        "working as designed, not a failure.",
    "no_proposal_turn_limit":
        "The model ran out of turns before proposing or declining. A defect — "
        "raise max_turns_per_ticker or trim the tool payloads.",
    "malformed_proposal":
        "The proposal violated the tool schema and never reached the gate.",
    "invalid_proposal":
        "The proposal was well-formed but named contracts that do not exist or "
        "cannot be quoted — usually a strike read from a stale chain.",
}


def latest_by_ticker(events: list[dict], types: tuple[str, ...]) -> dict[str, dict]:
    """Most recent matching event per underlying."""
    out: dict[str, dict] = {}
    for event in events:  # events are newest-first
        ticker = event.get("ticker")
        if event.get("event_type") in types and ticker and ticker not in out:
            out[ticker] = event
    return out


latest_agent = latest_by_ticker(payload.events, AGENT_EVENTS)
latest_gate = latest_by_ticker(payload.events, ("trade_approved", "trade_rejected"))
latest_turns = {}
for event in payload.events:
    if event.get("event_type") == "agent_turn":
        latest_turns.setdefault(event.get("ticker"), []).append(event)

if not latest_agent:
    st.info(
        "No agent activity in the journal yet. Run "
        "`python cron_runner.py --dry-run --force --ticker SPY` to record a cycle."
    )

for ticker, event in latest_agent.items():
    kind = event.get("event_type", "")
    label, tone = OUTCOME_STYLE.get(kind, ("UNKNOWN", "neutral"))

    meta = [T.status_pill(label, tone)]
    turns = event.get("turns_used")
    if turns is not None:
        meta.append(T.status_pill(f"{turns} turns", "neutral"))

    cycle = next((e for e in payload.events if e.get("event_type") == "cycle_summary"), {})
    if cycle.get("model"):
        meta.append(T.status_pill(str(cycle.get("model", ""))[:44], "neutral"))
    if cycle.get("mode"):
        meta.append(T.status_pill(str(cycle["mode"]).upper().replace("_", " "), "neutral"))

    html(T.section_heading(ticker, f"agent-{ticker.lower()}", T.pill_row(meta)))

    left, right = st.columns([3, 2], gap="medium")

    with left:
        st.markdown("**The model's reasoning**")
        if kind == "agent_proposal":
            rationale = event.get("rationale") or ""
            st.markdown(
                f"> {T.esc(rationale)}" if rationale else "_No rationale recorded._"
            )
            # Flagged, never suppressed: a run full of numberless hand-waving
            # should be visible as such rather than quietly hidden.
            if rationale and not any(ch.isdigit() for ch in rationale):
                st.caption(
                    "This rationale cites no numbers, which is a weak rationale."
                )
            st.caption(
                f"Proposed {T.esc(str(event.get('underlying', ticker)))} "
                f"{event.get('short_strike')}/{event.get('long_strike')} "
                f"exp {T.esc(str(event.get('expiry', '')))}, "
                f"{event.get('contracts_requested')} contract(s) requested."
            )
        else:
            st.markdown(f"_{T.esc(OUTCOME_NOTE.get(kind, ''))}_")
            detail = event.get("detail") or ""
            if detail:
                st.markdown(f"> {T.esc(detail)}")
            last = event.get("last_message") or ""
            if last:
                st.caption("Model's last message:")
                st.markdown(f"> {T.esc(last[:600])}")

        calls = latest_turns.get(ticker, [])
        if calls:
            st.markdown("**Tools it called, in order**")
            lines = []
            for turn in reversed(calls):  # oldest first
                for call in turn.get("tool_calls", []):
                    args = call.get("arguments") or {}
                    summary = ", ".join(
                        f"{k}={v}" for k, v in list(args.items())[:3]
                    )
                    lines.append(T.terminal_line(
                        f"turn {turn.get('turn', '?')}",
                        "agent_turn",
                        f"{call.get('name', '?')}({summary[:110]})",
                    ))
            html(T.terminal(lines, show_cursor=False))

    with right:
        st.markdown("**The gate's verdict**")
        verdict = latest_gate.get(ticker)
        if not verdict:
            st.markdown(
                "_The gate never ran — the proposal did not survive validation._"
                if kind in ("malformed_proposal", "invalid_proposal")
                else "_No proposal reached the gate._"
            )
        else:
            approved = verdict.get("event_type") == "trade_approved"
            html(T.pill_row([
                T.status_pill("APPROVED" if approved else "REJECTED",
                              "ok" if approved else "bad"),
                T.status_pill(f"{verdict.get('contracts', 0)} contracts", "neutral"),
            ]))
            failing = set(verdict.get("failing_rules") or [])
            rows = []
            for check in verdict.get("checks", []):
                passed = check.get("passed")
                rows.append(T.rule_row(
                    check.get("rule", "").split("_")[0].replace("R", ""),
                    check.get("name", check.get("rule", "")),
                    f"limit {check.get('limit')}",
                    "pass" if passed else "fail",
                    f"observed {check.get('observed')}",
                ))
            html(T.rule_grid(rows))
            if failing:
                st.caption(f"Failing: {', '.join(sorted(failing))}")

    st.divider()


# ----------------------------------------------------------- risk gate

html(T.section_heading("Risk Gate", "risk-gate"))

gate_events = [e for e in payload.events
               if e.get("event_type") in ("trade_approved", "trade_rejected")]
latest_checks = {c["rule"]: c for c in gate_events[-1].get("checks", [])} if gate_events else {}

# Each rule carries a formatter for the limit the gate actually applied, so the
# panel can show the journalled threshold rather than whatever the config says
# today. Those two diverge whenever a threshold is tuned, and showing the
# current limit beside a historical pass/fail produces the worst kind of
# nonsense: a rule marked FAILED against a limit it plainly satisfies.
RULES = [
    ("R1", "R1_delta", "Short-leg delta cap",
     f"\u2264 {config.max_abs_delta:.2f}", lambda v: f"\u2264 {float(v):.2f}"),
    ("R2", "R2_notional", "Max loss within budget",
     f"\u2264 {config.max_loss_pct:.0%} of NAV", lambda v: f"\u2264 {money(float(v))}"),
    ("R3", "R3_portfolio_delta", "Portfolio delta exposure",
     f"\u2264 {config.max_portfolio_delta_pct:.0%} of NAV", lambda v: f"\u2264 {money(float(v))}"),
    ("R4", "R4_min_premium", "Minimum credit",
     f"\u2265 {money(config.min_credit_usd)}", lambda v: f"\u2265 {money(float(v))}"),
    ("R5", "R5_duplicate", "No duplicate strike", "none open", lambda v: "none open"),
    ("R6", "R6_min_dte", "Minimum days to expiry",
     f"\u2265 {config.min_dte}d", lambda v: f"\u2265 {float(v):.0f}d"),
    ("R7", "R7_max_dte", "Maximum days to expiry",
     f"\u2264 {config.max_dte}d", lambda v: f"\u2264 {float(v):.0f}d"),
    ("R8", "R8_kill_switch", "Daily drawdown kill-switch",
     f"\u2264 {config.kill_switch_pct:.0%}", lambda v: f"\u2264 {float(v):.0%}"),
    ("R9", "R9_buying_power", "Buying-power reserve",
     f"\u2265 {config.min_bp_reserve_pct:.0%}", lambda v: f"\u2265 {money(float(v))}"),
]

rule_rows = []
thresholds_are_historical = False

for number, key, name, configured, render_limit in RULES:
    check = latest_checks.get(key)
    if check is None:
        rule_rows.append(T.rule_row(number, name, configured, "idle", ""))
        continue

    state = "pass" if check.get("passed") else "fail"
    observed = "" if check.get("observed") is None else str(check["observed"])

    # Prefer the limit recorded with the verdict; it is the one that produced
    # this pass/fail. Fall back to the configured text when the journal predates
    # limits being recorded.
    threshold = configured
    if check.get("limit") is not None:
        try:
            threshold = render_limit(check["limit"])
        except (TypeError, ValueError):
            threshold = str(check["limit"])
        if threshold != configured:
            thresholds_are_historical = True

    rule_rows.append(T.rule_row(number, name, threshold, state, observed))

html(T.rule_grid(rule_rows))

if thresholds_are_historical:
    st.caption(
        "Thresholds above are the ones applied at evaluation time. One or more "
        "has since been changed in `config/risk_config.json`, so they will not "
        "all match the sidebar until the next cycle runs."
    )

# --------------------------------------------------------- performance

html(T.section_heading("Performance", "performance"))

approval = (f"{stats['approval_rate']:.0f}%" if stats["approval_rate"] is not None else "—")
win = (f"{stats['win_rate']:.0f}%" if stats["win_rate"] is not None else "—")
realized_accent, realized_class = T.pnl_classes(stats["realized_pnl"])

html(T.card_grid([
    T.metric_card("Win rate", win, "secondary", "t-secondary",
                  sub="Closed positions only", icon_name="check"),
    T.metric_card("Realized P&L", money(stats["realized_pnl"]), realized_accent,
                  realized_class, icon_name="trend"),
    T.metric_card("Positions closed", str(stats["positions_closed"]), "neutral",
                  "t-on-surface", icon_name="box"),
    T.metric_card("Orders filled", str(stats["orders_filled"]), "neutral", "t-on-surface",
                  sub=f"{stats['dry_runs']} dry-run cycles excluded", icon_name="box"),
    T.metric_card("Avg credit", money(stats["avg_credit"]), "tertiary", "t-tertiary",
                  icon_name="cash"),
    T.metric_card("Gate approval rate", approval, "primary", "t-primary",
                  sub=f"{stats['approvals']} approved / {stats['rejections']} rejected",
                  icon_name="shield"),
]))

_dry = stats["dry_runs"]
summary = (
    f"**{stats['orders_filled']}** order{'' if stats['orders_filled'] == 1 else 's'} "
    f"submitted · **{stats['positions_closed']}** "
    f"position{'' if stats['positions_closed'] == 1 else 's'} closed · "
    f"**{money(stats['realized_pnl'])}** realized P&L"
)
if _dry:
    summary += f" · _{_dry} dry-run cycle{'' if _dry == 1 else 's'} (not counted)_"
st.markdown(summary)

pnl_series = build_pnl_series(payload.events)

if pnl_series.empty:
    st.info(
        "No closed positions yet, so there is no realized P&L to plot. "
        "The curve starts once the first spread is closed — at the 50% profit "
        "target, the stop, or the 2-DTE floor."
    )
else:
    chart_col, detail_col = st.columns([3, 1])
    with chart_col:
        import plotly.graph_objects as go

        fig = go.Figure(go.Scatter(
            x=pnl_series["Closed at"], y=pnl_series["Cumulative P&L"],
            mode="lines+markers", line=dict(color=T.TOKENS["secondary"], width=2.4),
            marker=dict(size=6, color=T.TOKENS["secondary"]),
            fill="tozeroy", fillcolor="rgba(0,255,204,0.09)",
            hovertemplate="%{x|%d %b %H:%M}<br>$%{y:,.2f}<extra></extra>",
        ))
        fig.update_layout(
            height=270, margin=dict(l=0, r=0, t=26, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family="Space Grotesk, sans-serif",
                      color=T.TOKENS["on_surface_variant"], size=11),
            title=dict(text="Cumulative realized P&L", font=dict(size=12)),
            xaxis=dict(gridcolor="rgba(48,40,64,0.7)", zeroline=False),
            yaxis=dict(gridcolor="rgba(48,40,64,0.7)", zeroline=True,
                       zerolinecolor="rgba(90,80,104,0.8)", tickprefix="$"),
            showlegend=False, hovermode="x unified",
        )
        st.plotly_chart(fig, config={"displayModeBar": False}, width="stretch")

    with detail_col:
        best = pnl_series["Trade P&L"].max()
        worst = pnl_series["Trade P&L"].min()
        avg = pnl_series["Trade P&L"].mean()
        html("".join([
            T.metric_card("Best trade", money(best), *T.pnl_classes(best)),
            T.metric_card("Worst trade", money(worst), *T.pnl_classes(worst)),
            T.metric_card("Avg per trade", money(avg), *T.pnl_classes(avg)),
        ]))

    with st.expander("Per-trade breakdown"):
        st.dataframe(
            pnl_series.assign(**{
                "Closed at": pnl_series["Closed at"].dt.strftime("%Y-%m-%d %H:%M")
            }),
            width="stretch", hide_index=True,
        )

# -------------------------------------------------------------- market

html(T.section_heading("Market", "market"))

market_rows, market_error = safe(get_market_state)

if market_rows:
    def fmt(value, spec, dash="—"):
        return dash if value is None else format(value, spec)

    rows = [[
        f'<span class="oa-ticker">{T.esc(r["ticker"])}</span>',
        fmt(r.get("spot"), ",.2f"),
        fmt(r.get("atm_iv"), ".2%"),
        fmt(r.get("iv_rank"), ".1f"),
        T.regime_pill(r.get("regime", "UNKNOWN")),
        f'<span class="oa-muted">{T.esc(r.get("iv_rank_source", "—"))}</span>',
    ] for r in market_rows]

    html(T.table(["Ticker", "Spot", "ATM IV", "IV Rank", "Regime", "Source"],
                 rows, "Market status"))

    if any(r.get("iv_rank_source") == "rv_proxy" for r in market_rows):
        st.caption(
            "IV rank marked `rv_proxy` is a cold-start estimate: today's ATM implied "
            "volatility ranked against the past year of realized volatility. It switches "
            "to a true IV percentile once 20 sessions of history have accumulated."
        )
else:
    html(unavailable(f"Market data could not be fetched — {str(market_error)[:180]}."))

# ------------------------------------------------------------- journal

html(T.section_heading("Trade Journal", "journal"))

if payload.source == SOURCE_NONE:
    st.info(
        "No journal found. Run `python cron_runner.py --dry-run --force` locally, "
        "or configure the data branch for a deployed instance."
    )

tabs = st.tabs(["Recent activity", "Rejections", "Fills & exits", "Raw"])
events = events_all[:200]


def describe(event: dict) -> str:
    """One line of terminal text for a journal event."""
    kind = event.get("event_type", "")
    execution = event.get("execution") or {}
    spread = event.get("spread") or {}
    ticker = event.get("ticker", "")
    if kind == "cycle_summary":
        return (f"cycle {event.get('run_id', '')[:8]} [{event.get('mode', '?')}] "
                f"approved={event.get('approved', 0)} rejected={event.get('rejected', 0)} "
                f"skipped={event.get('skipped', 0)}")
    if kind in ("trade_approved", "trade_rejected"):
        return f"{ticker} {event.get('reason', '')}"
    if kind == "analysis":
        ctx = event.get("market_context") or {}
        return (f"{ticker} spot={ctx.get('spot', '?')} iv_rank={ctx.get('iv_rank', '?')} "
                f"regime={ctx.get('regime', '?')}")
    if kind == "spread_candidates":
        return f"{ticker} {event.get('count', 0)} spreads built"
    if kind == "position_exit":
        return (f"{ticker} exit — {event.get('exit_reason', '')} "
                f"realized={money(execution.get('realized_pnl', 0))}")
    if kind in ("order_filled", "order_submitted", "order_dry_run", "order_failed"):
        strikes = (f"{spread.get('sell_strike')}/{spread.get('buy_strike')}"
                   if spread.get("sell_strike") else "")
        return f"{ticker} {strikes} {execution.get('message', kind)}"
    return f"{ticker} {event.get('reason') or event.get('error') or kind}"


with tabs[0]:
    if not events:
        html(T.terminal([
            T.terminal_line("", "analysis", "System initialized."),
            T.terminal_line("", "analysis", "Awaiting first cycle..."),
        ]))
    else:
        html(T.terminal([
            T.terminal_line(str(e.get("timestamp", ""))[11:19],
                            e.get("event_type", ""), describe(e))
            for e in events[:60]
        ]))

with tabs[1]:
    rejections = [e for e in events if is_rejection(e)]
    if not rejections:
        st.info("No rejections recorded yet. Every spread the gate has seen passed all nine rules.")
    else:
        st.caption(
            "Every trade the risk gate blocked, with the rule that stopped it, its "
            "threshold, and the value that breached it. This is the gate doing its job."
        )
        for event in rejections[:15]:
            spread = event.get("spread") or {}
            failing = [c for c in event.get("checks", []) if not c.get("passed")]
            title = (
                f"{event.get('ticker', '?')} "
                f"{spread.get('sell_strike', '?')}/{spread.get('buy_strike', '?')} · "
                f"{str(event.get('timestamp', ''))[:19].replace('T', ' ')}"
            )
            if not failing:
                html(T.rejection_card("—", "No rule detail recorded", title,
                                      rejection_reason(event), "—", "—"))
                continue
            for check in failing:
                html(T.rejection_card(
                    check.get("rule", "?").split("_")[0],
                    check.get("name", check.get("rule", "")),
                    title,
                    check.get("detail", rejection_reason(event)),
                    "—" if check.get("observed") is None else str(check["observed"]),
                    "—" if check.get("limit") is None else str(check["limit"]),
                ))

with tabs[2]:
    trades = [e for e in events if e.get("event_type") in (
        "order_filled", "order_submitted", "order_dry_run", "position_exit", "order_failed")]
    if not trades:
        st.info("No orders or exits recorded yet.")
    else:
        rows = []
        for event in trades:
            spread = event.get("spread") or {}
            execution = event.get("execution") or {}
            rows.append([
                T.esc(str(event.get("timestamp", ""))[:19].replace("T", " ")),
                T.esc(event.get("event_type", "")),
                f'<span class="oa-ticker">{T.esc(event.get("ticker", ""))}</span>',
                T.esc(f'{spread.get("sell_strike")}/{spread.get("buy_strike")}'
                      if spread.get("sell_strike") else execution.get("symbol", "—")),
                T.esc(execution.get("contracts", "—")),
                T.esc(spread.get("net_credit", "—")),
                T.esc(spread.get("max_loss", "—")),
                T.esc(execution.get("status", "—")),
            ])
        html(T.table(["Time", "Event", "Ticker", "Strikes", "Qty", "Credit",
                      "Max loss", "Status"], rows, "Fills & exits"))

with tabs[3]:
    st.caption(f"Last 25 raw journal entries · source: {payload.source}")
    st.json(events[:25], expanded=False)

html(
    f'<div class="oa-sub" style="text-align:center;margin-top:22px">'
    f'Read-only console · journal source: {T.esc(payload.source)} '
    f'({T.esc(payload.age_label)}) · '
    f'{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S UTC}</div>'
)
