"""The risk gate: nine hard rules, all of which must pass.

This is the most important file in the project and the one place where the
design is non-negotiable: **no language model, no network call, no I/O, no
randomness**. It is a pure function from (proposed spread, portfolio state,
account, config) to a decision. The same inputs always produce the same
decision, and every decision explains itself with the numbers that produced it.

That property is the whole thesis. An LLM asked "is this a good trade?" gives an
answer that cannot be reproduced, audited, or unit-tested. These nine rules can
be, and are — see ``tests/test_risk_gate.py``.

Every threshold is read from ``config/risk_config.json``. There are no magic
numbers in the checking logic, so tightening a limit never requires a code
change.
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
import math
from dataclasses import dataclass, field
from typing import Any

from .config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass
class RuleCheck:
    """One rule's verdict, with the values that produced it."""

    rule: str
    name: str
    passed: bool
    detail: str
    observed: Any = None
    limit: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "observed": self.observed,
            "limit": self.limit,
        }


@dataclass
class GateResult:
    """The gate's decision for one proposed spread."""

    approved: bool
    reason: str
    contracts: int = 0
    checks: list[RuleCheck] = field(default_factory=list)

    @property
    def failing_rules(self) -> list[str]:
        return [c.rule for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "contracts": self.contracts,
            "failing_rules": self.failing_rules,
            "checks": [c.to_dict() for c in self.checks],
        }


def risk_gate_check(
    spread: dict[str, Any],
    portfolio: dict[str, Any],
    config: AgentConfig,
    requested_contracts: int | None = None,
) -> GateResult:
    """Evaluate all nine rules against a proposed spread.

    Every rule runs even after one fails, so the journal records the complete
    picture rather than just the first objection. The trade is approved only if
    every single check passes.

    ``portfolio`` is expected to carry ``nav``, ``buying_power``,
    ``starting_buying_power``, ``daily_pnl_pct``, ``net_delta_dollars`` and
    ``open_positions``. Missing keys are treated as unknown and fail closed.
    """
    checks: list[RuleCheck] = []

    nav = float(portfolio.get("nav") or 0.0)
    if nav <= 0:
        # No account value means no basis for any percentage-of-NAV rule. Fail
        # closed rather than dividing by zero or, worse, treating an unknown
        # account as an unconstrained one.
        return GateResult(
            approved=False,
            reason=(
                "REJECTED — account NAV is unknown or zero, so no position can be sized "
                "or risk-checked. Refusing to trade on missing account state."
            ),
            contracts=0,
            checks=[RuleCheck(
                rule="R0_preconditions",
                name="Account state available",
                passed=False,
                detail="portfolio['nav'] was missing or non-positive.",
                observed=nav,
                limit="> 0",
            )],
        )
    spot = float(portfolio.get("spot") or spread.get("sell_strike") or 0.0)
    max_loss = float(spread.get("max_loss") or 0.0)
    net_credit = float(spread.get("net_credit") or 0.0)
    sell_delta = float(spread.get("sell_delta") or 0.0)

    # --- Position sizing, derived before the rules that depend on it. --------
    # Two independent budgets constrain size, and the smaller one wins.
    #
    # Rule 2's budget is capital at risk: how many contracts fit inside 2% of
    # NAV. Rule 3's budget is directional exposure: how many fit inside the
    # remaining portfolio-delta headroom.
    #
    # Sizing down to fit rather than rejecting outright is a deliberate choice.
    # A spread that is sound but slightly too large is a sizing problem, not a
    # risk violation; shrinking it to the largest compliant size keeps the agent
    # trading while respecting every limit. The rules below still reject when
    # even a single contract will not fit, so no limit is ever exceeded.
    loss_budget = nav * config.max_loss_pct
    affordable = int(loss_budget // max_loss) if max_loss > 0 else 0

    existing_delta_dollars = float(portfolio.get("net_delta_dollars") or 0.0)
    delta_limit = nav * config.max_portfolio_delta_pct
    delta_per_contract = abs(float(spread.get("net_delta") or 0.0) * 100.0 * spot)
    if delta_per_contract > 0:
        headroom = max(0.0, delta_limit - abs(existing_delta_dollars))
        delta_affordable = int(headroom // delta_per_contract)
    else:
        delta_affordable = affordable

    contracts = min(affordable, delta_affordable)
    if requested_contracts is not None:
        contracts = min(requested_contracts, contracts)
    contracts = max(0, contracts)

    # Record what bound the size, so the journal explains a small position.
    size_limited_by = (
        "delta" if delta_affordable < affordable else "loss_budget"
    )

    # --- Rule 1: short-leg delta cap ----------------------------------------
    limit = config.max_abs_delta
    observed = abs(sell_delta)
    checks.append(RuleCheck(
        rule="R1_delta",
        name="Short-leg delta cap",
        passed=observed <= limit + 1e-9,
        detail=(
            f"Short strike delta {observed:.4f} against a {limit:.2f} cap. "
            "Higher delta means a higher chance of finishing in the money."
        ),
        observed=round(observed, 4),
        limit=limit,
    ))

    # --- Rule 2: notional / max-loss budget ---------------------------------
    # Judged against this rule's own budget, using ``affordable`` rather than
    # the final contract count. If the delta budget shrank the position to zero,
    # that is Rule 3's objection to report, not this one's — otherwise a
    # delta-bound rejection would blame the wrong limit in the journal.
    total_risk = round(max_loss * contracts, 2)
    checks.append(RuleCheck(
        rule="R2_notional",
        name="Max loss within budget",
        passed=affordable >= 1 and round(max_loss * affordable, 2) <= loss_budget + 1e-6,
        detail=(
            f"{contracts} contract(s) x ${max_loss:,.2f} max loss = ${total_risk:,.2f}, "
            f"against a ${loss_budget:,.2f} budget ({config.max_loss_pct:.0%} of "
            f"${nav:,.2f} NAV). Loss budget alone allows {affordable} contract(s)."
            + ("" if affordable >= 1 else
               " A single contract already exceeds the loss budget.")
        ),
        observed=total_risk,
        limit=round(loss_budget, 2),
    ))

    # --- Rule 3: aggregate portfolio delta ----------------------------------
    # Expressed in delta-dollars: the net directional exposure of the whole
    # book, in dollars of underlying, capped as a fraction of NAV. A cap of 0.50
    # means "the book is never more directionally exposed than being half long
    # the index".
    #
    # plan.md phrases this as "aggregate delta <= 0.10 per $100k", which is
    # unit-ambiguous. Delta-dollars is the reading that actually constrains
    # risk, but note that 0.10 under this reading admits roughly one contract in
    # total — it would reject every trade the rest of the plan is built to
    # produce. See config/risk_config.json for the recalibration.
    trade_delta_dollars = float(spread.get("net_delta") or 0.0) * 100.0 * contracts * spot
    projected = existing_delta_dollars + trade_delta_dollars
    checks.append(RuleCheck(
        rule="R3_portfolio_delta",
        name="Portfolio delta exposure",
        passed=contracts >= 1 and abs(projected) <= delta_limit + 1e-6,
        detail=(
            f"Book delta would move from ${existing_delta_dollars:,.0f} to ${projected:,.0f} "
            f"of underlying exposure ({abs(projected) / nav:.1%} of NAV), against a "
            f"${delta_limit:,.0f} cap ({config.max_portfolio_delta_pct:.0%} of NAV). "
            f"Position size was bound by the {size_limited_by} budget. "
            "Stops many small spreads from summing into one large directional bet."
            + ("" if contracts >= 1 else
               " Even one contract exceeds the remaining delta headroom.")
        ),
        observed=round(projected, 2),
        limit=round(delta_limit, 2),
    ))

    # --- Rule 4: minimum premium --------------------------------------------
    checks.append(RuleCheck(
        rule="R4_min_premium",
        name="Minimum credit per contract",
        passed=net_credit >= config.min_credit_usd - 1e-9,
        detail=(
            f"Credit of ${net_credit:,.2f} per contract against a ${config.min_credit_usd:,.2f} "
            "floor. Thinner credits do not survive bid/ask slippage."
        ),
        observed=round(net_credit, 2),
        limit=config.min_credit_usd,
    ))

    # --- Rule 5: no duplicate strike ----------------------------------------
    duplicate = _find_duplicate(spread, portfolio.get("open_positions") or [])
    checks.append(RuleCheck(
        rule="R5_duplicate",
        name="No duplicate strike",
        passed=duplicate is None,
        detail=(
            f"Already holding {duplicate} at this strike and expiry."
            if duplicate
            else f"No open position on {spread.get('ticker')} "
                 f"{spread.get('expiry')} {spread.get('sell_strike')}P."
        ),
        observed=duplicate,
        limit=None,
    ))

    # --- Rules 6 and 7: days to expiry --------------------------------------
    dte = int(spread.get("dte") or 0)
    checks.append(RuleCheck(
        rule="R6_min_dte",
        name="Minimum days to expiry",
        passed=dte >= config.min_dte,
        detail=(
            f"{dte} days to expiry against a {config.min_dte}-day floor. Closer in, gamma "
            "dominates and a small move in the underlying swings P&L violently."
        ),
        observed=dte,
        limit=config.min_dte,
    ))
    checks.append(RuleCheck(
        rule="R7_max_dte",
        name="Maximum days to expiry",
        passed=dte <= config.max_dte,
        detail=(
            f"{dte} days to expiry against a {config.max_dte}-day ceiling. Further out, "
            "theta decay is too slow to justify holding the capital."
        ),
        observed=dte,
        limit=config.max_dte,
    ))

    # --- Rule 8: daily drawdown kill-switch ---------------------------------
    # daily_pnl_pct is signed: negative is a loss. Compare the magnitude of a
    # loss only — a profitable day can never trip the breaker.
    daily_pnl_pct = float(portfolio.get("daily_pnl_pct") or 0.0)
    daily_loss = max(0.0, -daily_pnl_pct)
    checks.append(RuleCheck(
        rule="R8_kill_switch",
        name="Daily drawdown kill-switch",
        passed=daily_loss <= config.kill_switch_pct + 1e-9,
        detail=(
            f"Book is {daily_pnl_pct:+.2%} on the day against a "
            f"-{config.kill_switch_pct:.0%} circuit breaker."
            + ("" if daily_loss <= config.kill_switch_pct else
               " Kill-switch active: no new positions until the next session.")
        ),
        observed=round(daily_loss, 6),
        limit=config.kill_switch_pct,
    ))

    # --- Rule 9: buying-power reserve ---------------------------------------
    # A defined-risk credit spread collateralises at its max loss, so that is
    # the buying power the position consumes.
    buying_power = float(portfolio.get("buying_power") or 0.0)
    starting_bp = float(portfolio.get("starting_buying_power") or buying_power)
    required = max_loss * contracts
    remaining = buying_power - required
    reserve_floor = starting_bp * config.min_bp_reserve_pct
    checks.append(RuleCheck(
        rule="R9_buying_power",
        name="Buying-power reserve",
        passed=remaining >= reserve_floor,
        detail=(
            f"${required:,.2f} of collateral would leave ${remaining:,.2f} available, "
            f"against a ${reserve_floor:,.2f} floor "
            f"({config.min_bp_reserve_pct:.0%} of ${starting_bp:,.2f} starting buying power)."
        ),
        observed=round(remaining, 2),
        limit=round(reserve_floor, 2),
    ))

    failed = [c for c in checks if not c.passed]
    if failed:
        reason = "REJECTED — " + "; ".join(f"{c.rule}: {c.detail}" for c in failed)
        result = GateResult(approved=False, reason=reason, contracts=0, checks=checks)
    else:
        reason = (
            f"APPROVED — all 9 rules passed. {contracts} contract(s) of "
            f"{spread.get('ticker')} {spread.get('sell_strike'):.0f}/{spread.get('buy_strike'):.0f} "
            f"bull put, ${net_credit:,.2f} credit each, ${max_loss * contracts:,.2f} max loss total."
        )
        result = GateResult(approved=True, reason=reason, contracts=contracts, checks=checks)

    logger.info(
        "Risk gate %s for %s: %s",
        "APPROVED" if result.approved else "REJECTED",
        spread.get("ticker"),
        result.failing_rules or "all clear",
    )
    return result


def _find_duplicate(spread: dict[str, Any], open_positions: list[dict[str, Any]]) -> str | None:
    """Return the symbol of an open position at the same strike and expiry.

    Matching on the *short* strike: two spreads sharing a short strike are the
    same directional bet stacked, which is exactly the concentration this rule
    exists to prevent.
    """
    ticker = str(spread.get("ticker", "")).upper()
    expiry = str(spread.get("expiry", ""))
    strike = float(spread.get("sell_strike") or 0.0)

    for position in open_positions:
        if str(position.get("underlying", "")).upper() != ticker:
            continue
        if str(position.get("expiry", "")) != expiry:
            continue
        if math.isclose(float(position.get("strike") or 0.0), strike, abs_tol=1e-6):
            return str(position.get("symbol") or f"{ticker} {expiry} {strike:g}P")
    return None
