"""One trading cycle: research, propose, validate, gate, and only then execute.

The control flow is the safety argument made concrete. The model runs a
tool-calling loop over read-only data and may call `propose_spread`. That call
does not place anything — it ends the model's turn and returns control here.
What follows is entirely deterministic:

    proposal -> schema validation -> leg validation -> nine-rule gate -> order

and only the last step touches a write tool, called by this module, never by the
model.

**Four ways a cycle ends without an order**, kept distinct because two are
healthy and two are defects:

``no_proposal_declined``
    The model judged conditions poor and said so. Working as designed.
``no_proposal_turn_limit``
    It ran out of turns before proposing. A defect — tune the cap.
``malformed_proposal``
    Schema violation.
``invalid_proposal``
    Well-formed, but names contracts that do not exist or cannot be quoted. The
    model picks strikes from a chain it saw several turns ago, and quotes move.
    Without this check that surfaces later as a confusing Rule 4 credit failure,
    which points at the credit rather than at the stale strike.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import date, datetime
from typing import Any

from .adapters import (
    MCPBrokerView,
    adapt_account,
    adapt_bars,
    adapt_chain,
    adapt_clock,
    adapt_positions,
    adapt_spot,
    bars_request,
    chain_request,
    diagnose_bracket,
    occ_symbol,
    option_positions,
    order_request,
    summarise_chain,
)
from .config import AgentConfig
from .iv import atm_implied_volatility, compute_iv_rank
from .journal import (
    EVENT_INVALID_PROPOSAL,
    EVENT_MALFORMED_PROPOSAL,
    EVENT_NO_PROPOSAL_DECLINED,
    EVENT_NO_PROPOSAL_TURN_LIMIT,
    TradeJournal,
)
from .llm import LLMClient, LLMError
from .mcp_client import MCPClient, MCPError
from .options_calculator import calculate_options_opportunities
from .position_manager import build_portfolio_state, decide_exits
from .prompts import PROMPT_VERSION, analyst_messages
from .risk_gate import risk_gate_check
from .tools import PROPOSE_SPREAD, MalformedProposal, Proposal, parse_proposal

logger = logging.getLogger(__name__)

# Tool results are fed back to the model as JSON. An option chain is far larger
# than any turn needs, and eight turns of them would crowd out the reasoning.
MAX_TOOL_RESULT_CHARS = 6000


class Orchestrator:
    """Runs one cycle across the configured underlyings."""

    def __init__(
        self,
        mcp: MCPClient,
        llm: LLMClient,
        config: AgentConfig,
        journal: TradeJournal,
        dry_run: bool = True,
    ):
        self.mcp = mcp
        self.llm = llm
        self.config = config
        self.journal = journal
        self.dry_run = dry_run

    # ------------------------------------------------------------- the cycle

    async def run_cycle(
        self,
        tickers: list[str] | None = None,
        force: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or hashlib.sha1(
            datetime.now().isoformat().encode()
        ).hexdigest()[:12]
        tickers = tickers or self.config.underlyings
        started = time.monotonic()

        summary: dict[str, Any] = {
            "run_id": run_id,
            "mode": "dry_run" if self.dry_run else "live",
            "prompt_version": PROMPT_VERSION,
            "provider": self.llm.chain[0].name,
            "model": self.llm.chain[0].model,
            "tickers": tickers,
            "outcomes": {},
            "approved": 0,
            "submitted": 0,
            "rejected": 0,
            "exits": 0,
            "errors": [],
        }

        # 1. Market-hours gate, from Alpaca's own clock rather than local time.
        #
        # This and the portfolio read are the two calls that must survive a bad
        # credential or a transient Alpaca outage without a traceback: an
        # unattended run needs a journalled reason in the log, not a stack dump
        # that cron mails into the void.
        try:
            clock = adapt_clock(await self.mcp.call_read("get_clock", {}))
        except MCPError as exc:
            reason = _explain(exc)
            logger.error("Could not reach Alpaca: %s", reason)
            self.journal.log_error(run_id, "-", reason)
            summary["errors"].append(reason)
            summary["skipped"] = "alpaca_unreachable"
            self.journal.log_cycle(run_id, summary)
            return summary

        if not clock.is_open and not force:
            self.journal.log_skip(run_id, "-", "Market closed; no cycle run.")
            summary["skipped"] = "market_closed"
            self.journal.log_cycle(run_id, summary)
            logger.info("Market closed, skipping cycle %s", run_id)
            return summary

        # 2. Portfolio state — feeds the kill switch and Rules 3, 5 and 9.
        try:
            portfolio = await self.build_portfolio()
        except MCPError as exc:
            reason = _explain(exc)
            logger.error("Could not read the account: %s", reason)
            self.journal.log_error(run_id, "-", reason)
            summary["errors"].append(reason)
            summary["skipped"] = "portfolio_unavailable"
            self.journal.log_cycle(run_id, summary)
            return summary

        summary["nav"] = portfolio["nav"]
        summary["daily_pnl_pct"] = portfolio["daily_pnl_pct"]

        # 3. Exits before entries: capital freed here is available to the gate,
        #    and a spread past its stop should not wait on an analysis pass.
        summary["exits"] = await self.manage_exits(run_id, portfolio)

        # 4. Analyse EVERY underlying. Only execution is capped — capping
        #    analysis silently skipped QQQ and IWM in the previous build.
        opened = 0
        max_new = int(self.config.execution.get("max_new_positions_per_cycle", 1))

        for ticker in tickers:
            try:
                outcome = await asyncio.wait_for(
                    self.analyse_ticker(
                        run_id, ticker, portfolio,
                        may_execute=opened < max_new,
                    ),
                    timeout=self.config.cycle_timeout_seconds,
                )
            except asyncio.TimeoutError:
                outcome = {"outcome": "timeout", "detail": (
                    f"Analysis exceeded {self.config.cycle_timeout_seconds}s."
                )}
                self.journal.log_no_proposal(
                    run_id, ticker, EVENT_NO_PROPOSAL_TURN_LIMIT,
                    outcome["detail"], turns_used=0,
                )
            except (MCPError, LLMError) as exc:
                logger.error("%s failed: %s", ticker, exc)
                outcome = {"outcome": "error", "detail": str(exc)}
                self.journal.log_error(run_id, ticker, str(exc))
                summary["errors"].append(f"{ticker}: {exc}")

            summary["outcomes"][ticker] = outcome
            if outcome.get("approved"):
                summary["approved"] += 1
            if outcome.get("rejected"):
                summary["rejected"] += 1
            if outcome.get("submitted"):
                summary["submitted"] += 1
                opened += 1

        summary["duration_seconds"] = round(time.monotonic() - started, 1)
        self.journal.log_cycle(run_id, summary)
        return summary

    # ---------------------------------------------------------- portfolio

    async def build_portfolio(self) -> dict[str, Any]:
        """Account and open positions, in the shape the risk gate expects.

        Delegates to the ported `build_portfolio_state` so the aggregate-Greeks
        arithmetic exists in exactly one place — the dashboard renders the same
        shape, and Rule 3 reads its `net_delta_dollars`.
        """
        account = await self.mcp.call_read("get_account_info", {})
        positions = await self.mcp.call_read("get_all_positions", {})

        # Live Greeks for the held contracts: a position's delta today is not
        # the delta it was opened at.
        held = [
            str(p.symbol) for p in option_positions(adapt_positions(positions))
        ]
        snapshots: dict[str, Any] = {}
        if held:
            try:
                snapshots = await self.mcp.call_read("get_option_snapshot", {
                    "symbols": ",".join(held), "feed": self.config.options_feed,
                })
            except MCPError as exc:
                logger.warning("Could not refresh position Greeks: %s", exc)

        spots: dict[str, float] = {}
        for ticker in {str(p.symbol)[:3] for p in option_positions(adapt_positions(positions))}:
            try:
                spots[ticker] = adapt_spot(await self.mcp.call_read(
                    "get_stock_latest_trade", {"symbols": ticker, "feed": "iex"}))
            except MCPError:
                spots[ticker] = 0.0

        view = MCPBrokerView(account, positions, snapshots)
        return build_portfolio_state(view, self.config, spots)

    async def manage_exits(self, run_id: str, portfolio: dict[str, Any]) -> int:
        """Close spreads that have hit a target, a stop, or the DTE floor.

        Spread-level throughout: `decide_exits` groups legs before judging, so a
        short leg in profit can never be closed without its long wing.
        """
        exits = decide_exits(portfolio, self.config)
        for group in exits:
            for symbol in group["symbols"]:
                if self.dry_run:
                    result = {"success": True, "dry_run": True, "status": "dry_run",
                              "symbol": symbol}
                else:
                    try:
                        response = await self.mcp.call_write(
                            "close_position", {"symbol_or_asset_id": symbol}
                        )
                        result = {"success": True, "dry_run": False,
                                  "status": "submitted", "symbol": symbol,
                                  "response": response}
                    except MCPError as exc:
                        logger.error("Could not close %s: %s", symbol, exc)
                        result = {"success": False, "dry_run": False,
                                  "status": "failed", "symbol": symbol, "error": str(exc)}
                self.journal.log_exit(run_id, group, group["exit_reason"], result)
        return len(exits)

    # ------------------------------------------------------- one underlying

    async def analyse_ticker(
        self,
        run_id: str,
        ticker: str,
        portfolio: dict[str, Any],
        may_execute: bool = True,
    ) -> dict[str, Any]:
        """Research, model loop, validation, gate, and possibly an order."""
        context = await self.market_context(run_id, ticker)

        proposal, outcome, turns_used, last_message = await self.run_model_loop(
            run_id, ticker, context
        )

        if proposal is None:
            self.journal.log_no_proposal(
                run_id, ticker, outcome["event"], outcome["detail"],
                turns_used=turns_used, last_message=last_message,
            )
            return {"outcome": outcome["event"], "detail": outcome["detail"],
                    "turns_used": turns_used}

        self.journal.log_agent_proposal(run_id, ticker, proposal.to_journal(), turns_used)

        # Leg validation, before the gate. A well-formed proposal can still name
        # contracts that no longer quote.
        spread, error = await self.validate_legs(proposal, context)
        if spread is None:
            self.journal.log_no_proposal(
                run_id, ticker, EVENT_INVALID_PROPOSAL, error,
                turns_used=turns_used, last_message=last_message,
                proposal=proposal.to_journal(),
            )
            return {"outcome": EVENT_INVALID_PROPOSAL, "detail": error,
                    "turns_used": turns_used}

        # The gate now only ever sees proposals whose legs are real, so a rule
        # failure always means a genuine rule breach.
        verdict = risk_gate_check(
            spread, portfolio, self.config,
            requested_contracts=proposal.contracts_requested,
        )
        self.journal.log_gate_decision(
            run_id, ticker, spread, verdict.approved, verdict.reason,
            [c.to_dict() for c in verdict.checks], verdict.contracts,
        )

        if not verdict.approved:
            return {"outcome": "rejected", "rejected": True,
                    "detail": verdict.reason, "turns_used": turns_used,
                    "failing_rules": verdict.failing_rules}

        if not may_execute:
            return {"outcome": "approved_not_executed", "approved": True,
                    "detail": "Already opened the maximum new positions this cycle.",
                    "turns_used": turns_used}

        result = await self.execute(run_id, ticker, spread, verdict.contracts)
        return {
            "outcome": "submitted" if result.get("success") and not self.dry_run
            else "approved",
            "approved": True,
            "submitted": bool(result.get("success") and not self.dry_run),
            "turns_used": turns_used,
            "execution": result,
        }

    async def market_context(self, run_id: str, ticker: str) -> dict[str, Any]:
        """Fetch and journal the deterministic view of one underlying.

        This runs regardless of what the model does, because the dashboard's
        market panel needs IV rank for every underlying whether or not a trade
        was proposed.
        """
        spot = adapt_spot(await self.mcp.call_read(
            "get_stock_latest_trade", {"symbols": ticker, "feed": "iex"}))

        args = chain_request(
            ticker, spot,
            feed=self.config.options_feed,
            bracket_pct=self.config.chain["strike_bracket_pct"],
            min_dte=self.config.min_dte,
            max_dte=self.config.max_dte,
            limit=self.config.chain["limit"],
        )
        chain = adapt_chain(await self.mcp.call_read("get_option_chain", args))
        bars = adapt_bars(await self.mcp.call_read("get_stock_bars", bars_request(ticker)))

        atm_iv = atm_implied_volatility(chain, spot)
        iv = compute_iv_rank(
            ticker=ticker, atm_iv=atm_iv, closes=list(bars["close"]),
            history_path=self.config.paths["iv_history"], iv_config=self.config.iv,
        )

        candidates = calculate_options_opportunities(chain, ticker, spot, self.config)
        diagnosis = diagnose_bracket(
            chain, self.config.delta_range,
            (args["strike_price_gte"], args["strike_price_lte"]),
            args["limit"], len(candidates),
        )
        if diagnosis.is_defect:
            logger.warning("%s chain bracket problem: %s", ticker, diagnosis.detail)

        context = {
            "ticker": ticker,
            "spot": spot,
            "atm_iv": atm_iv,
            "iv_rank": iv.get("iv_rank"),
            "iv_rank_source": iv.get("iv_rank_source"),
            "regime": iv.get("regime"),
            "feed": self.config.options_feed,
            "chain_size": len(chain),
            "candidates": len(candidates),
            "bracket": diagnosis.to_journal(),
        }
        self.journal.log_analysis(run_id, ticker, context)

        context["_chain"] = chain
        context["_candidates"] = candidates
        return context

    # -------------------------------------------------------- the model loop

    async def run_model_loop(
        self, run_id: str, ticker: str, context: dict[str, Any]
    ) -> tuple[Proposal | None, dict[str, str], int, str]:
        """Let the model research and either propose or decline.

        Returns ``(proposal, outcome, turns_used, last_message)`` where a
        ``None`` proposal carries the reason in ``outcome``.
        """
        tools = self.mcp.tools_for_model()
        messages = analyst_messages(ticker, self._cycle_context(context))
        last_message = ""

        for turn in range(1, self.config.max_turns_per_ticker + 1):
            result = await self.llm.chat(messages, tools)

            if result.switched_from:
                self.journal.log_provider_switch(
                    run_id, ticker, result.switched_from, result.provider, result.model
                )

            if result.text:
                last_message = result.text

            if not result.wants_tools:
                # No tool calls and no proposal: the model has finished talking,
                # which for this prompt means it declined.
                self.journal.log_agent_turn(run_id, ticker, turn, result)
                return None, {
                    "event": EVENT_NO_PROPOSAL_DECLINED,
                    "detail": "The model declined to propose a trade.",
                }, turn, last_message

            # A proposal ends the loop; control returns to deterministic code.
            for call in result.tool_calls:
                if call.name == PROPOSE_SPREAD:
                    self.journal.log_agent_turn(run_id, ticker, turn, result)
                    try:
                        return parse_proposal(call.arguments), {}, turn, last_message
                    except MalformedProposal as exc:
                        return None, {
                            "event": EVENT_MALFORMED_PROPOSAL,
                            "detail": str(exc),
                        }, turn, last_message

            messages.append({
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": _dumps(c.arguments)}}
                    for c in result.tool_calls
                ],
            })

            tool_results = []
            for call in result.tool_calls:
                payload, size = await self._dispatch(call)
                tool_results.append({"name": call.name, "result_chars": size})
                messages.append({
                    "role": "tool", "tool_call_id": call.id, "name": call.name,
                    "content": payload,
                })

            self.journal.log_agent_turn(run_id, ticker, turn, result, tool_results)

        return None, {
            "event": EVENT_NO_PROPOSAL_TURN_LIMIT,
            "detail": (
                f"Reached the {self.config.max_turns_per_ticker}-turn cap without "
                "proposing or declining."
            ),
        }, self.config.max_turns_per_ticker, last_message

    async def _dispatch(self, call: Any) -> tuple[str, int]:
        """Run one model-requested read tool and render its result for the model."""
        try:
            payload = await self.mcp.call_read(call.name, call.arguments)
        except MCPError as exc:
            # A failed tool call is information the model can act on — a bad
            # argument it can correct — not a reason to abandon the cycle.
            return _dumps({"error": str(exc)}), 0

        # Chains are compacted rather than truncated. A bracketed SPY chain is
        # ~450,000 characters; truncating it leaves only the lowest strikes,
        # which carry no Greeks, so the model cannot find anything tradeable and
        # re-queries until its turns run out.
        if call.name in ("get_option_chain", "get_option_snapshot"):
            payload = summarise_chain(payload, delta_range=self.config.delta_range)

        text = _dumps(payload)
        size = len(text)
        if size > MAX_TOOL_RESULT_CHARS:
            text = (
                text[:MAX_TOOL_RESULT_CHARS]
                + f"... [truncated at {MAX_TOOL_RESULT_CHARS} of {size} chars]"
            )
        return text, size

    def _cycle_context(self, context: dict[str, Any]) -> str:
        """A short factual preamble so the model is not blind on turn one.

        Deliberately thin: it names the underlying's regime but not a strike, so
        the model still has to fetch the chain to propose anything.
        """
        parts = [f"Spot is {context['spot']}."]
        if context.get("iv_rank") is not None:
            parts.append(
                f"IV rank is {context['iv_rank']} (source: {context['iv_rank_source']})."
            )
        parts.append(f"Use feed=\"{context['feed']}\" for all option data calls.")
        return " ".join(parts)

    # --------------------------------------------------------- leg validation

    async def validate_legs(
        self, proposal: Proposal, context: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """Confirm both legs exist and are quotable, and price the spread.

        Returns the spread dict the risk gate consumes, or ``(None, reason)``.
        Everything priced here comes from a *fresh* snapshot rather than the
        chain the model saw, because the two can differ by several turns.
        """
        sell_symbol = occ_symbol(proposal.underlying, proposal.expiry, proposal.short_strike)
        buy_symbol = occ_symbol(proposal.underlying, proposal.expiry, proposal.long_strike)

        try:
            snapshots = adapt_chain(await self.mcp.call_read("get_option_snapshot", {
                "symbols": f"{sell_symbol},{buy_symbol}",
                "feed": self.config.options_feed,
            }))
        except MCPError as exc:
            return None, f"Could not quote the proposed legs: {exc}"

        for label, symbol in (("short", sell_symbol), ("long", buy_symbol)):
            if symbol not in snapshots:
                return None, (
                    f"The {label} leg {symbol} does not exist. The model proposed a "
                    f"strike that is not listed for {proposal.expiry}."
                )

        short, long_leg = snapshots[sell_symbol], snapshots[buy_symbol]

        for label, symbol, snap in (("short", sell_symbol, short), ("long", buy_symbol, long_leg)):
            quote = getattr(snap, "latest_quote", None)
            bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
            ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
            if bid <= 0 or ask <= 0:
                return None, (
                    f"The {label} leg {symbol} has no two-sided quote "
                    f"(bid {bid}, ask {ask}); it cannot be traded at any price."
                )
            if getattr(snap, "greeks", None) is None:
                return None, (
                    f"The {label} leg {symbol} has no Greeks, so its delta cannot be "
                    "checked against Rule 1."
                )

        short_bid = float(short.latest_quote.bid_price)
        short_ask = float(short.latest_quote.ask_price)
        long_bid = float(long_leg.latest_quote.bid_price)
        long_ask = float(long_leg.latest_quote.ask_price)

        # Sell the short at its bid, buy the long at its ask: the conservative
        # fill, so a spread that only works at mid does not reach the gate.
        net_credit = round((short_bid - long_ask) * 100.0, 2)
        mid_credit = round(
            ((short_bid + short_ask) / 2 - (long_bid + long_ask) / 2) * 100.0, 2
        )
        width = proposal.width
        max_loss = round(width * 100.0 - net_credit, 2)

        if net_credit <= 0:
            return None, (
                f"The proposed spread collects no credit: short bid {short_bid} minus "
                f"long ask {long_ask} is {net_credit / 100:.2f}. This is a debit, not a "
                "credit spread."
            )

        short_delta = float(short.greeks.delta)
        long_delta = float(long_leg.greeks.delta)

        return {
            "type": "bull_put",
            "ticker": proposal.underlying,
            "expiry": proposal.expiry,
            "dte": proposal.dte(),
            "sell_symbol": sell_symbol,
            "buy_symbol": buy_symbol,
            "sell_strike": proposal.short_strike,
            "buy_strike": proposal.long_strike,
            "spread_width": width,
            "sell_bid": short_bid, "sell_ask": short_ask,
            "buy_bid": long_bid, "buy_ask": long_ask,
            "sell_delta": short_delta,
            "buy_delta": long_delta,
            # Long minus short — the sign fix from PLAN.md section 3.1. A bull
            # put spread is net LONG delta; computing it the other way labels
            # every one of them bearish and feeds Rule 3 a flipped number.
            "net_delta": round(long_delta - short_delta, 4),
            "sell_iv": getattr(short, "implied_volatility", None),
            "net_credit": net_credit,
            "mid_credit": mid_credit,
            "max_loss": max_loss,
            "return_on_risk_pct": round(net_credit / max_loss * 100.0, 2) if max_loss else 0.0,
            "prob_profit": round(100.0 - abs(short_delta) * 100.0, 2),
            "breakeven": round(proposal.short_strike - net_credit / 100.0, 2),
            "rationale": proposal.rationale,
        }, ""

    # -------------------------------------------------------------- execution

    async def execute(
        self, run_id: str, ticker: str, spread: dict[str, Any], contracts: int
    ) -> dict[str, Any]:
        """Place the approved spread. Called by the orchestrator, never the model."""
        trade_date = date.today().isoformat()
        client_order_id = build_client_order_id(spread, trade_date, contracts)
        limit_price = compute_limit_price(spread, self.config)

        base = {
            "client_order_id": client_order_id,
            "contracts": contracts,
            "limit_price": limit_price,
            "credit_target": round(abs(limit_price) * 100.0 * contracts, 2),
            "max_loss_total": round(float(spread["max_loss"]) * contracts, 2),
            "sell_symbol": spread["sell_symbol"],
            "buy_symbol": spread["buy_symbol"],
            "submitted_at": datetime.now().astimezone().isoformat(),
        }

        if self.dry_run:
            result = {
                **base, "success": True, "dry_run": True, "status": "dry_run",
                "message": (
                    f"DRY RUN — would sell {contracts}x {ticker} "
                    f"{spread['sell_strike']:g}/{spread['buy_strike']:g} put spread "
                    f"for a {abs(limit_price):.2f} credit per contract."
                ),
            }
            # A separate event type on purpose: journalling dry runs as
            # order_submitted showed judges "8 orders placed" with nothing traded.
            self.journal.log_execution(run_id, ticker, spread, result)
            return result

        # Idempotency: a crashed cycle must not double-place on retry.
        existing = await self._find_existing_order(client_order_id)
        if existing is not None:
            result = {**base, "success": True, "dry_run": False, "duplicate": True,
                      "status": "duplicate",
                      "message": "An order with this client_order_id already existed."}
            self.journal.log_execution(run_id, ticker, spread, result)
            return result

        try:
            response = await self.mcp.call_write("place_option_order", order_request(
                sell_symbol=spread["sell_symbol"],
                buy_symbol=spread["buy_symbol"],
                contracts=contracts,
                limit_price=limit_price,
                client_order_id=client_order_id,
                time_in_force=str(self.config.execution.get("time_in_force", "day")),
            ))
            result = {
                **base, "success": True, "dry_run": False, "status": "submitted",
                "order_id": str((response or {}).get("id", "")),
                "message": (
                    f"Submitted {contracts}x {ticker} "
                    f"{spread['sell_strike']:g}/{spread['buy_strike']:g} put spread."
                ),
            }
        except (MCPError, ValueError) as exc:
            logger.error("Order submission failed for %s: %s", ticker, exc)
            result = {**base, "success": False, "dry_run": False, "status": "failed",
                      "error": str(exc), "message": f"Order failed: {exc}"}

        self.journal.log_execution(run_id, ticker, spread, result)
        return result

    async def _find_existing_order(self, client_order_id: str) -> Any | None:
        """Look for an order this cycle may already have placed before crashing.

        A lookup failure returns None rather than raising: being unable to check
        must not block a legitimate order. The deterministic client_order_id is
        the real guard — Alpaca rejects a duplicate of it regardless.
        """
        try:
            orders = await self.mcp.call_read("get_orders", {"status": "all", "limit": 500})
        except MCPError as exc:
            logger.warning("Could not check for an existing order: %s", exc)
            return None

        for order in orders if isinstance(orders, list) else []:
            if isinstance(order, dict) and order.get("client_order_id") == client_order_id:
                return order
        return None


# ------------------------------------------------------------------ helpers


def build_client_order_id(spread: dict[str, Any], trade_date: str, contracts: int) -> str:
    """A deterministic id for this exact trade.

    Same spread, same day, same size produces the same id, so a retry after a
    crash is recognised by Alpaca as a duplicate rather than opening a second
    position. The field is capped at 128 characters; the hash keeps it well
    inside that.
    """
    payload = "|".join([
        str(spread.get("ticker", "")),
        str(spread.get("expiry", "")),
        str(spread.get("sell_strike", "")),
        str(spread.get("buy_strike", "")),
        str(trade_date),
        str(contracts),
    ])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
    return f"oa-{spread.get('ticker', 'X')}-{digest}"


def compute_limit_price(spread: dict[str, Any], config: AgentConfig) -> float:
    """The limit price for the spread, signed for Alpaca's mleg convention.

    Returns a **negative** number: for multi-leg orders a negative limit is a
    credit to be received and a positive one is a debit to be paid.

    The price starts from the mid of the two legs and gives back a configured
    amount of slippage. Sending the mid exactly tends not to fill; sending the
    conservative bid/ask credit gives away the whole edge.
    """
    execution = config.execution
    mode = str(execution.get("limit_price_mode", "mid")).lower()
    slippage = float(execution.get("limit_price_slippage", 0.0))

    if mode == "conservative":
        credit_per_share = float(spread["net_credit"]) / 100.0
    else:
        credit_per_share = float(spread.get("mid_credit", spread["net_credit"])) / 100.0

    credit_per_share = max(0.01, credit_per_share - slippage)
    return -round(credit_per_share, 2)


def _explain(exc: Exception) -> str:
    """Turn a transport error into one line a human can act on.

    A 401 buried in an nginx HTML error page is the most likely thing to greet
    someone reading cron.log, and "Unauthorized - <html><head><title>..." is not
    a useful log line at 9:35 on a Monday.
    """
    text = str(exc)
    if "401" in text or "Unauthorized" in text:
        return (
            "Alpaca rejected the credentials (HTTP 401). Check ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY in .env, and that they belong to a paper account "
            "when ALPACA_PAPER_TRADE=true."
        )
    if "403" in text and "OPRA" in text:
        return (
            "Alpaca returned 403 for the options feed. Pass feed=\"indicative\"; "
            "the OPRA feed needs a signed agreement."
        )
    if "403" in text:
        return f"Alpaca refused the request (HTTP 403): {text[:200]}"
    # Collapse any HTML error page to its first meaningful line.
    if "<html>" in text.lower():
        head = text.split("<html>")[0].strip()
        return (head or text)[:200]
    return text[:300]


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)
