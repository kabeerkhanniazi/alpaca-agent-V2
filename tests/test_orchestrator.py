"""The cycle: mediation, the four no-order outcomes, validation, and execution.

Everything runs against a fake MCP server and a scripted LLM. No test here can
reach Alpaca or a model provider, so the whole file is deterministic and fast.

The assertions that matter most are the ones about what does *not* happen: that
a rejected proposal places nothing, that a dry run places nothing, and that the
write tool is only ever reached from the orchestrator.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from agent.adapters import occ_symbol
from agent.journal import (
    EVENT_INVALID_PROPOSAL,
    EVENT_MALFORMED_PROPOSAL,
    EVENT_NO_PROPOSAL_DECLINED,
    EVENT_NO_PROPOSAL_TURN_LIMIT,
    TradeJournal,
)
from agent.llm import ChatResult, ToolCall
from agent.orchestrator import Orchestrator, build_client_order_id, compute_limit_price
from agent.tools import PROPOSE_SPREAD

EXPIRY = (date.today() + timedelta(days=9)).isoformat()
SHORT = occ_symbol("SPY", EXPIRY, 750.0)
LONG = occ_symbol("SPY", EXPIRY, 745.0)


def snapshot(delta: float, bid: float, ask: float, iv: float = 0.14) -> dict:
    return {
        "greeks": {"delta": delta, "gamma": 0.01, "theta": -0.16, "vega": 0.21},
        "impliedVolatility": iv,
        "latestQuote": {"bp": bid, "ap": ask, "bs": 10, "as": 10},
        "latestTrade": {"p": (bid + ask) / 2},
    }


class FakeMCP:
    """Stands in for MCPClient, with the same two-door call surface."""

    def __init__(self, *, positions=None, snapshots=None, is_open=True, orders=None):
        self.is_open = is_open
        self.positions = positions or []
        self.orders = orders or []
        self.snapshots = snapshots if snapshots is not None else {
            SHORT: snapshot(-0.19, 2.02, 2.05),
            LONG: snapshot(-0.12, 1.30, 1.33),
        }
        self.writes: list[tuple[str, dict]] = []
        self.reads: list[str] = []

    # -- the model's door
    async def call_read(self, name, arguments=None):
        self.reads.append(name)
        arguments = arguments or {}
        if name == "get_clock":
            return {"is_open": self.is_open, "next_open": "2026-08-24T09:30:00-04:00",
                    "next_close": "2026-08-24T16:00:00-04:00",
                    "timestamp": "2026-08-22T11:48:15.043465234-04:00"}
        if name == "get_account_info":
            return {"equity": "100000", "last_equity": "100000", "cash": "100000",
                    "buying_power": "400000", "portfolio_value": "100000",
                    "options_trading_level": 3, "status": "ACTIVE"}
        if name == "get_all_positions":
            return self.positions
        if name == "get_orders":
            return self.orders
        if name == "get_stock_latest_trade":
            return {"trades": {"SPY": {"p": 765.55}}}
        if name == "get_stock_bars":
            return {"bars": {"SPY": [
                {"o": 760, "h": 766, "l": 758, "c": 760 + (i % 7), "v": 1_000_000,
                 "t": f"2026-0{1 + i % 8}-1{i % 9}T04:00:00Z"}
                for i in range(260)
            ]}}
        if name in ("get_option_chain", "get_option_snapshot"):
            requested = str(arguments.get("symbols", "")).split(",")
            if name == "get_option_snapshot":
                return {"snapshots": {s: self.snapshots[s]
                                      for s in requested if s in self.snapshots}}
            return {"snapshots": dict(self.snapshots)}
        raise AssertionError(f"unexpected read tool {name}")

    # -- the orchestrator's door
    async def call_write(self, name, arguments=None):
        self.writes.append((name, arguments or {}))
        return {"id": "order-123", "status": "accepted"}

    def tools_for_model(self):
        return [{"type": "function", "function": {"name": "get_clock",
                                                  "description": "", "parameters": {}}}]

    def missing_read_tools(self):
        return []


class ScriptedLLM:
    """Replays a fixed list of ChatResults, one per turn."""

    def __init__(self, *turns: ChatResult):
        self.turns = list(turns)
        self.calls = 0

        class _P:
            name, model = "fake", "fake-model"

        self.chain = [_P()]

    async def chat(self, messages, tools=None, client=None):
        result = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return result

    def describe(self):
        return "fake:fake-model"


def proposal_turn(**overrides) -> ChatResult:
    args = {
        "underlying": "SPY", "short_strike": 750.0, "long_strike": 745.0,
        "expiry": EXPIRY, "contracts_requested": 4,
        "rationale": "IV rank 38, short delta -0.19, credit $69, 9 DTE, no SPY exposure.",
    }
    args.update(overrides)
    return ChatResult(text="", tool_calls=[ToolCall("c1", PROPOSE_SPREAD, args)],
                      provider="fake", model="fake-model")


def read_turn(name="get_clock") -> ChatResult:
    return ChatResult(text="", tool_calls=[ToolCall("c0", name, {})],
                      provider="fake", model="fake-model")


def decline_turn(text="IV is too low to be paid for this risk.") -> ChatResult:
    return ChatResult(text=text, tool_calls=[], provider="fake", model="fake-model")


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    return TradeJournal(tmp_path / "journal.jsonl")


def events(journal: TradeJournal) -> list[dict]:
    if not journal.path.exists():
        return []
    return [json.loads(line) for line in journal.path.read_text().splitlines() if line.strip()]


def types(journal: TradeJournal) -> list[str]:
    return [e["event_type"] for e in events(journal)]


def build(config, journal, llm, mcp=None, dry_run=True):
    return Orchestrator(mcp or FakeMCP(), llm, config, journal, dry_run=dry_run)


# ------------------------------------------------------------ the happy path


async def test_a_full_cycle_reaches_the_gate_and_journals_everything(config, journal):
    agent = build(config, journal, ScriptedLLM(proposal_turn()))
    summary = await agent.run_cycle(["SPY"], force=True)

    recorded = types(journal)
    assert "analysis" in recorded
    assert "agent_turn" in recorded
    assert "agent_proposal" in recorded
    assert "trade_approved" in recorded
    assert "cycle_summary" in recorded
    assert summary["approved"] == 1


async def test_the_rationale_is_journalled_verbatim(config, journal):
    """It is the evidence a model reasoned, so it is never truncated."""
    rationale = "IV rank 38, short delta -0.19, credit $69 on a $5 width, 9 DTE."
    agent = build(config, journal, ScriptedLLM(proposal_turn(rationale=rationale)))
    await agent.run_cycle(["SPY"], force=True)

    proposal = next(e for e in events(journal) if e["event_type"] == "agent_proposal")
    assert proposal["rationale"] == rationale


async def test_every_rule_is_journalled_with_observed_and_limit(config, journal):
    """A rejection must be explainable without re-deriving anything."""
    agent = build(config, journal, ScriptedLLM(proposal_turn()))
    await agent.run_cycle(["SPY"], force=True)

    verdict = next(e for e in events(journal) if e["event_type"] in
                   ("trade_approved", "trade_rejected"))
    assert len(verdict["checks"]) >= 9
    for check in verdict["checks"]:
        assert "rule" in check and "passed" in check
        assert "observed" in check and "limit" in check


async def test_the_cycle_summary_records_provider_model_and_mode(config, journal):
    """So the write-up can state accurately what drove the trading."""
    agent = build(config, journal, ScriptedLLM(proposal_turn()))
    await agent.run_cycle(["SPY"], force=True)

    summary = next(e for e in events(journal) if e["event_type"] == "cycle_summary")
    assert summary["provider"] == "fake"
    assert summary["model"] == "fake-model"
    assert summary["mode"] == "dry_run"
    assert summary["prompt_version"]


# --------------------------------------------- ★ nothing executes without approval


async def test_a_dry_run_never_touches_a_write_tool(config, journal):
    mcp = FakeMCP()
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp, dry_run=True)
    await agent.run_cycle(["SPY"], force=True)

    assert mcp.writes == [], "a dry run must not reach any write tool"
    assert "order_dry_run" in types(journal)
    assert "order_submitted" not in types(journal)


async def test_a_live_run_places_the_order_through_the_orchestrator(config, journal):
    mcp = FakeMCP()
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp, dry_run=False)
    await agent.run_cycle(["SPY"], force=True)

    assert len(mcp.writes) == 1
    name, args = mcp.writes[0]
    assert name == "place_option_order"
    assert float(args["limit_price"]) < 0, "a credit spread submits a negative limit"
    assert args["order_class"] == "mleg"
    assert len(args["legs"]) == 2


async def test_a_rejected_proposal_places_nothing(config, journal):
    """The rejection moment: reasoning, a verdict, and no order."""
    config.risk["premium"]["min_credit_usd"] = 10_000.0  # Rule 4 cannot pass
    mcp = FakeMCP()
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp, dry_run=False)
    summary = await agent.run_cycle(["SPY"], force=True)

    assert mcp.writes == []
    assert "trade_rejected" in types(journal)
    assert summary["rejected"] == 1

    verdict = next(e for e in events(journal) if e["event_type"] == "trade_rejected")
    assert "R4_min_premium" in verdict["failing_rules"]


# ------------------------------------------------- the four no-order outcomes


async def test_a_declining_model_is_recorded_as_healthy(config, journal):
    """Working as designed: the model judged conditions poor and said why."""
    reason = "IV rank 12 is too low to be paid for this risk."
    agent = build(config, journal, ScriptedLLM(decline_turn(reason)))
    summary = await agent.run_cycle(["SPY"], force=True)

    assert EVENT_NO_PROPOSAL_DECLINED in types(journal)
    entry = next(e for e in events(journal)
                 if e["event_type"] == EVENT_NO_PROPOSAL_DECLINED)
    assert entry["is_defect"] is False
    assert reason in entry["last_message"]
    assert summary["approved"] == 0


async def test_exhausting_the_turn_cap_is_recorded_as_a_defect(config, journal):
    """A different signal entirely from declining, and it must look different."""
    config.agent["max_turns_per_ticker"] = 3
    agent = build(config, journal, ScriptedLLM(read_turn()))  # never proposes
    await agent.run_cycle(["SPY"], force=True)

    entry = next(e for e in events(journal)
                 if e["event_type"] == EVENT_NO_PROPOSAL_TURN_LIMIT)
    assert entry["is_defect"] is True
    assert entry["turns_used"] == 3


async def test_declining_and_running_out_of_turns_are_distinguishable(config, journal, tmp_path):
    """The whole point of splitting the event: one is healthy, one is not."""
    declined = TradeJournal(tmp_path / "a.jsonl")
    await build(config, declined, ScriptedLLM(decline_turn())).run_cycle(["SPY"], force=True)

    config.agent["max_turns_per_ticker"] = 2
    exhausted = TradeJournal(tmp_path / "b.jsonl")
    await build(config, exhausted, ScriptedLLM(read_turn())).run_cycle(["SPY"], force=True)

    assert EVENT_NO_PROPOSAL_DECLINED in types(declined)
    assert EVENT_NO_PROPOSAL_TURN_LIMIT in types(exhausted)
    assert types(declined) != types(exhausted)


async def test_a_malformed_proposal_is_recorded_and_the_cycle_continues(config, journal):
    agent = build(config, journal, ScriptedLLM(proposal_turn(long_strike=760.0)))
    summary = await agent.run_cycle(["SPY"], force=True)

    entry = next(e for e in events(journal)
                 if e["event_type"] == EVENT_MALFORMED_PROPOSAL)
    assert entry["is_defect"] is True
    assert "must be BELOW" in entry["detail"]
    assert summary["approved"] == 0


# ------------------------------------------------------- ★ leg validation


async def test_a_proposal_for_a_nonexistent_strike_is_caught_before_the_gate(config, journal):
    """The stale-strike case.

    Without this check it reaches the gate and fails Rule 4 on credit, which
    points the reader at the price rather than at the strike that never existed.
    """
    mcp = FakeMCP()
    agent = build(config, journal, ScriptedLLM(proposal_turn(short_strike=999.0)), mcp)
    await agent.run_cycle(["SPY"], force=True)

    recorded = types(journal)
    assert EVENT_INVALID_PROPOSAL in recorded
    assert "trade_rejected" not in recorded, "the gate should never have seen this"
    assert "trade_approved" not in recorded

    entry = next(e for e in events(journal) if e["event_type"] == EVENT_INVALID_PROPOSAL)
    assert "does not exist" in entry["detail"]


async def test_a_leg_with_no_two_sided_quote_is_caught(config, journal):
    """A strike with no bid cannot be sold at any price."""
    mcp = FakeMCP(snapshots={SHORT: snapshot(-0.19, 0.0, 2.05),
                             LONG: snapshot(-0.12, 1.30, 1.33)})
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp)
    await agent.run_cycle(["SPY"], force=True)

    entry = next(e for e in events(journal) if e["event_type"] == EVENT_INVALID_PROPOSAL)
    assert "two-sided quote" in entry["detail"]
    assert mcp.writes == []


async def test_a_leg_without_greeks_is_caught(config, journal):
    """Rule 1 reads short-leg delta; without Greeks it cannot be evaluated."""
    bad = snapshot(-0.19, 2.02, 2.05)
    del bad["greeks"]
    mcp = FakeMCP(snapshots={SHORT: bad, LONG: snapshot(-0.12, 1.30, 1.33)})
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp)
    await agent.run_cycle(["SPY"], force=True)

    entry = next(e for e in events(journal) if e["event_type"] == EVENT_INVALID_PROPOSAL)
    assert "no Greeks" in entry["detail"]


async def test_a_spread_that_collects_no_credit_is_caught(config, journal):
    """Short bid below long ask is a debit, whatever the model called it."""
    mcp = FakeMCP(snapshots={SHORT: snapshot(-0.19, 1.00, 1.05),
                             LONG: snapshot(-0.12, 1.30, 1.40)})
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp)
    await agent.run_cycle(["SPY"], force=True)

    entry = next(e for e in events(journal) if e["event_type"] == EVENT_INVALID_PROPOSAL)
    assert "debit" in entry["detail"]


async def test_validation_prices_from_a_fresh_quote_not_the_models_numbers(config, journal):
    """Quotes move between the model reading a chain and proposing from it."""
    mcp = FakeMCP(snapshots={SHORT: snapshot(-0.19, 1.80, 1.85),
                             LONG: snapshot(-0.12, 1.30, 1.35)})
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp)
    await agent.run_cycle(["SPY"], force=True)

    verdict = next(e for e in events(journal) if e["event_type"] == "trade_approved")
    # short bid 1.80 - long ask 1.35 = 0.45 -> $45, not whatever the model claimed.
    assert verdict["spread"]["net_credit"] == pytest.approx(45.0)


# --------------------------------------------------------------- the gate path


async def test_the_market_hours_gate_skips_the_cycle(config, journal):
    mcp = FakeMCP(is_open=False)
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp)
    summary = await agent.run_cycle(["SPY"], force=False)

    assert summary["skipped"] == "market_closed"
    assert "cycle_skipped" in types(journal)
    assert mcp.writes == []


async def test_force_overrides_the_market_hours_gate(config, journal):
    mcp = FakeMCP(is_open=False)
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp)
    summary = await agent.run_cycle(["SPY"], force=True)

    assert "skipped" not in summary
    assert "agent_proposal" in types(journal)


async def test_execution_is_capped_but_analysis_is_not(config, journal):
    """Capping analysis silently skipped QQQ and IWM in the previous build."""
    config.options["execution"]["max_new_positions_per_cycle"] = 1
    mcp = FakeMCP()
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp, dry_run=False)
    summary = await agent.run_cycle(["SPY", "QQQ", "IWM"], force=True)

    analysed = [e["ticker"] for e in events(journal) if e["event_type"] == "analysis"]
    assert set(analysed) == {"SPY", "QQQ", "IWM"}, "every underlying must be analysed"
    assert len(mcp.writes) == 1, "only one new position may be opened"
    assert summary["submitted"] == 1


# ------------------------------------------------------------- order details


def test_the_client_order_id_is_deterministic():
    """A crashed cycle must not double-place on retry."""
    spread = {"ticker": "SPY", "expiry": EXPIRY, "sell_strike": 750.0, "buy_strike": 745.0}
    first = build_client_order_id(spread, "2026-08-24", 4)
    assert first == build_client_order_id(spread, "2026-08-24", 4)
    assert first != build_client_order_id(spread, "2026-08-25", 4)
    assert first != build_client_order_id(spread, "2026-08-24", 5)
    assert len(first) <= 128


async def test_an_existing_order_is_adopted_rather_than_duplicated(config, journal):
    spread = {"ticker": "SPY", "expiry": EXPIRY, "sell_strike": 750.0, "buy_strike": 745.0}
    existing = build_client_order_id(spread, date.today().isoformat(), 4)

    mcp = FakeMCP(orders=[{"client_order_id": existing, "id": "already-there"}])
    agent = build(config, journal, ScriptedLLM(proposal_turn()), mcp, dry_run=False)
    await agent.run_cycle(["SPY"], force=True)

    assert mcp.writes == [], "the existing order should have been adopted"
    execution = next(e for e in events(journal) if "order" in e["event_type"])
    assert execution["execution"]["duplicate"] is True


def test_the_limit_price_is_always_a_credit(config):
    """Positive would be a debit — an order paying to open a position that collects."""
    assert compute_limit_price({"net_credit": 69.0, "mid_credit": 71.0}, config) < 0
    assert compute_limit_price({"net_credit": 1.0, "mid_credit": 1.0}, config) < 0


# ------------------------------------------------------------------ failures


async def test_a_tool_error_is_given_back_to_the_model_not_raised(config, journal):
    """A bad argument is something the model can correct on the next turn."""
    class FailingMCP(FakeMCP):
        async def call_read(self, name, arguments=None):
            if name == "get_option_chain" and arguments and arguments.get("symbols"):
                raise AssertionError("unreachable")
            return await super().call_read(name, arguments)

    agent = build(config, journal, ScriptedLLM(read_turn(), proposal_turn()), FailingMCP())
    summary = await agent.run_cycle(["SPY"], force=True)
    assert summary["approved"] == 1


async def test_one_ticker_failing_does_not_stop_the_others(config, journal):
    """An unattended run must not lose two underlyings to one bad response."""
    calls = {"n": 0}

    class FlakyMCP(FakeMCP):
        async def call_read(self, name, arguments=None):
            if name == "get_stock_latest_trade":
                calls["n"] += 1
                if calls["n"] == 1:
                    from agent.mcp_client import MCPError
                    raise MCPError("upstream data outage")
            return await super().call_read(name, arguments)

    agent = build(config, journal, ScriptedLLM(proposal_turn()), FlakyMCP())
    summary = await agent.run_cycle(["SPY", "QQQ"], force=True)

    assert summary["outcomes"]["SPY"]["outcome"] == "error"
    assert summary["outcomes"]["QQQ"]["approved"] is True
    assert "error" in types(journal)
