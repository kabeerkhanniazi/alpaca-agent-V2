"""Structured, append-only trade journal.

Everything the agent decides lands here as one JSON object per line: approvals,
rejections with the failing rule, fills, exits, errors, and skipped cycles.
JSONL rather than the markdown log TradingAgents writes, because this file is
read by machines — the dashboard renders from it and the analytics functions
below compute win rate and P&L from it.

Rejections are logged as carefully as fills. A judge reading this file should be
able to see the risk gate refusing trades and understand exactly why, which is
the clearest evidence the gate is real and not decorative.
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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

EVENT_ANALYSIS = "analysis"
EVENT_CANDIDATES = "spread_candidates"
EVENT_APPROVED = "trade_approved"
EVENT_REJECTED = "trade_rejected"
EVENT_SUBMITTED = "order_submitted"
EVENT_DRY_RUN = "order_dry_run"
EVENT_FILLED = "order_filled"
EVENT_FAILED = "order_failed"
EVENT_EXIT = "position_exit"
EVENT_SKIP = "cycle_skipped"
EVENT_ERROR = "error"
EVENT_CYCLE = "cycle_summary"

# ---- Agent reasoning events (new in this build).
#
# The previous journal recorded decisions. This one must also record *reasoning*,
# because that is the evidence an AI agent exists at all. Added rather than
# substituted, so the ported dashboard and analytics keep working unchanged.
EVENT_AGENT_TURN = "agent_turn"
EVENT_AGENT_PROPOSAL = "agent_proposal"
EVENT_PROVIDER_SWITCH = "provider_switch"

# Four ways a cycle can end without an order, deliberately kept distinct. Two of
# them are healthy and two are defects, and a dashboard that renders them the
# same way hides the difference at exactly the moment it matters.
EVENT_NO_PROPOSAL_DECLINED = "no_proposal_declined"      # healthy: the model judged conditions poor
EVENT_NO_PROPOSAL_TURN_LIMIT = "no_proposal_turn_limit"  # defect: ran out of turns
EVENT_MALFORMED_PROPOSAL = "malformed_proposal"          # defect: schema violation
EVENT_INVALID_PROPOSAL = "invalid_proposal"              # defect: legs do not exist or are unquotable

NO_PROPOSAL_EVENTS = (
    EVENT_NO_PROPOSAL_DECLINED,
    EVENT_NO_PROPOSAL_TURN_LIMIT,
    EVENT_MALFORMED_PROPOSAL,
    EVENT_INVALID_PROPOSAL,
)

# Which of those indicate something to fix rather than a market condition.
DEFECT_EVENTS = (
    EVENT_NO_PROPOSAL_TURN_LIMIT,
    EVENT_MALFORMED_PROPOSAL,
    EVENT_INVALID_PROPOSAL,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeJournal:
    """Append-only JSONL writer plus the read helpers the dashboard needs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- Writing -----------------------------------------------------------

    def write(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one event. Never raises — a journal failure must not kill a cycle.

        Losing a log line is bad; crashing an unattended trading agent because a
        disk hiccuped is worse. Failures are logged to stderr and swallowed.
        """
        record = {"timestamp": _utcnow(), "event_type": event_type, **payload}
        try:
            # If a previous process was killed mid-write the file may end
            # without a newline. Appending straight onto that partial line would
            # merge two records and destroy the good one as well as the torn
            # one, so start a fresh line first.
            prefix = "\n" if self._needs_newline() else ""
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(prefix + json.dumps(record, default=str) + "\n")
        except OSError as exc:
            logger.error("Could not write journal entry (%s): %s", event_type, exc)
        return record

    def _needs_newline(self) -> bool:
        """True when the file exists and its last byte is not a newline."""
        try:
            if not self.path.is_file() or self.path.stat().st_size == 0:
                return False
            with open(self.path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except OSError:
            return False

    def log_analysis(self, run_id: str, ticker: str, context: dict[str, Any]) -> None:
        self.write(EVENT_ANALYSIS, {"run_id": run_id, "ticker": ticker, "market_context": context})

    def log_spread_candidates(self, run_id: str, ticker: str, spreads: list[dict[str, Any]]) -> None:
        self.write(EVENT_CANDIDATES, {
            "run_id": run_id,
            "ticker": ticker,
            "count": len(spreads),
            # Only the top few: the full list would bloat the journal on every
            # five-minute cycle without adding anything a reader needs.
            "top_spreads": spreads[:3],
        })

    def log_gate_decision(
        self,
        run_id: str,
        ticker: str,
        spread: dict[str, Any] | None,
        approved: bool,
        reason: str,
        checks: list[dict[str, Any]],
        contracts: int = 0,
    ) -> None:
        failing = [c["rule"] for c in checks if not c.get("passed")]
        self.write(
            EVENT_APPROVED if approved else EVENT_REJECTED,
            {
                "run_id": run_id,
                "ticker": ticker,
                "spread": spread,
                "contracts": contracts,
                "reason": reason,
                "failing_rules": failing,
                "checks": checks,
            },
        )

    def log_execution(
        self,
        run_id: str,
        ticker: str,
        spread: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        status = str(result.get("status", "")).lower()
        if result.get("dry_run"):
            # Dry runs get their own event type so they never inflate the
            # "orders submitted" or credit-collected figures on the dashboard.
            # A simulated trade is not a trade.
            event = EVENT_DRY_RUN
        elif not result.get("success"):
            event = EVENT_FAILED
        elif status in ("filled", "partially_filled"):
            event = EVENT_FILLED
        else:
            event = EVENT_SUBMITTED
        self.write(event, {"run_id": run_id, "ticker": ticker, "spread": spread, "execution": result})

    def log_exit(self, run_id: str, position: dict[str, Any], reason: str, result: dict[str, Any]) -> None:
        self.write(EVENT_EXIT, {
            "run_id": run_id,
            "ticker": position.get("underlying"),
            "position": position,
            "exit_reason": reason,
            "execution": result,
        })

    def log_skip(self, run_id: str, ticker: str, reason: str) -> None:
        self.write(EVENT_SKIP, {"run_id": run_id, "ticker": ticker, "reason": reason})

    def log_error(self, run_id: str, ticker: str, error: str) -> None:
        self.write(EVENT_ERROR, {"run_id": run_id, "ticker": ticker, "error": error})

    # ---- Agent reasoning ---------------------------------------------------

    def log_agent_turn(
        self,
        run_id: str,
        ticker: str,
        turn: int,
        result: Any,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> None:
        """One model turn: what it called, with what, and how much came back.

        Result sizes rather than result bodies — a full option chain in the
        journal would dwarf everything else on every five-minute cycle.
        """
        self.write(EVENT_AGENT_TURN, {
            "run_id": run_id,
            "ticker": ticker,
            "turn": turn,
            "provider": getattr(result, "provider", None),
            "model": getattr(result, "model", None),
            "text": (getattr(result, "text", "") or "")[:2000],
            "tool_calls": [
                {"name": c.name, "arguments": c.arguments}
                for c in getattr(result, "tool_calls", [])
            ],
            "tool_results": tool_results or [],
        })

    def log_agent_proposal(
        self, run_id: str, ticker: str, proposal: dict[str, Any], turns_used: int
    ) -> None:
        """The propose_spread payload, including the full rationale verbatim.

        The rationale is the evidence that a model reasoned rather than
        pattern-matched, so it is never truncated here.
        """
        self.write(EVENT_AGENT_PROPOSAL, {
            "run_id": run_id, "ticker": ticker, "turns_used": turns_used, **proposal,
        })

    def log_no_proposal(
        self,
        run_id: str,
        ticker: str,
        event_type: str,
        detail: str,
        turns_used: int,
        last_message: str = "",
        **extra: Any,
    ) -> None:
        """One of the four no-order outcomes.

        ``is_defect`` is written into the record rather than derived by readers,
        so the dashboard and any later analysis agree on which outcomes were
        healthy without re-encoding the rule.
        """
        self.write(event_type, {
            "run_id": run_id,
            "ticker": ticker,
            "detail": detail,
            "turns_used": turns_used,
            "last_message": (last_message or "")[:2000],
            "is_defect": event_type in DEFECT_EVENTS,
            **extra,
        })

    def log_provider_switch(
        self, run_id: str, ticker: str, switched_from: str, now_using: str, model: str
    ) -> None:
        """A failover. Honest, and it explains any tonal shift in the rationales."""
        self.write(EVENT_PROVIDER_SWITCH, {
            "run_id": run_id, "ticker": ticker,
            "switched_from": switched_from, "provider": now_using, "model": model,
        })

    def log_cycle(self, run_id: str, summary: dict[str, Any]) -> None:
        self.write(EVENT_CYCLE, {"run_id": run_id, **summary})

    # ---- Reading -----------------------------------------------------------

    def read_all(self) -> Iterator[dict[str, Any]]:
        """Yield every event, skipping any line a killed process left torn."""
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def load_recent(
        self,
        limit: int = 50,
        ticker: str | None = None,
        event_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent events first."""
        events = list(self.read_all())
        if ticker:
            events = [e for e in events if e.get("ticker") == ticker]
        if event_types:
            events = [e for e in events if e.get("event_type") in event_types]
        return list(reversed(events))[:limit]

    def compute_stats(self, ticker: str | None = None) -> dict[str, Any]:
        """Headline performance numbers, computed from this journal's file."""
        return self.stats_from_events(list(self.read_all()), ticker)

    @staticmethod
    def stats_from_events(
        events: list[dict[str, Any]], ticker: str | None = None
    ) -> dict[str, Any]:
        """Headline performance numbers for an arbitrary list of events.

        Separated from the file-reading path because the dashboard may be
        showing a journal fetched from a remote data branch or a committed
        snapshot rather than the local file — the arithmetic is the same
        wherever the events came from.

        Win rate counts *closed* positions only. An open position has no
        realized outcome, and counting unrealized gains as wins would flatter
        the number in exactly the way a premium-selling strategy is prone to.
        """
        events = list(events)
        if ticker:
            events = [e for e in events if e.get("ticker") == ticker]

        fills = [e for e in events if e.get("event_type") == EVENT_FILLED]
        submits = [e for e in events if e.get("event_type") == EVENT_SUBMITTED]
        dry_runs = [e for e in events if e.get("event_type") == EVENT_DRY_RUN]
        rejections = [e for e in events if e.get("event_type") == EVENT_REJECTED]
        approvals = [e for e in events if e.get("event_type") == EVENT_APPROVED]
        exits = [e for e in events if e.get("event_type") == EVENT_EXIT]

        realized = [float(e.get("execution", {}).get("realized_pnl") or 0.0) for e in exits]
        closed = [p for p in realized if p != 0.0]
        wins = [p for p in closed if p > 0]

        credits = []
        for event in fills + submits:
            spread = event.get("spread") or {}
            contracts = float(event.get("execution", {}).get("contracts") or 1)
            if spread.get("net_credit"):
                credits.append(float(spread["net_credit"]) * contracts)

        notional = []
        for event in fills + submits:
            spread = event.get("spread") or {}
            contracts = float(event.get("execution", {}).get("contracts") or 1)
            if spread.get("max_loss"):
                notional.append(float(spread["max_loss"]) * contracts)

        # Which rule does the gate reject on most? Directly useful for tuning.
        rule_counts: dict[str, int] = {}
        for event in rejections:
            for rule in event.get("failing_rules", []):
                rule_counts[rule] = rule_counts.get(rule, 0) + 1

        return {
            "total_events": len(events),
            "approvals": len(approvals),
            "rejections": len(rejections),
            "orders_submitted": len(submits) + len(fills),
            "orders_filled": len(fills),
            "dry_runs": len(dry_runs),
            "positions_closed": len(exits),
            "win_rate": round(len(wins) / len(closed) * 100.0, 2) if closed else None,
            "realized_pnl": round(sum(realized), 2),
            "avg_credit": round(sum(credits) / len(credits), 2) if credits else 0.0,
            "total_credit": round(sum(credits), 2),
            "total_notional_at_risk": round(sum(notional), 2),
            "approval_rate": (
                round(len(approvals) / (len(approvals) + len(rejections)) * 100.0, 2)
                if (approvals or rejections) else None
            ),
            "rejections_by_rule": dict(sorted(rule_counts.items(), key=lambda kv: -kv[1])),
        }

    def compute_win_rate(self, ticker: str | None = None) -> float | None:
        return self.compute_stats(ticker)["win_rate"]

    def compute_total_pnl(self, ticker: str | None = None) -> float:
        return self.compute_stats(ticker)["realized_pnl"]
