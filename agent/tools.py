"""The synthetic `propose_spread` tool — the model's only route toward a trade.

`propose_spread` is not an Alpaca tool. It is a schema the agent invents and
appends to the read-only tool list, and calling it does not place anything: it
ends the model's turn and hands control back to deterministic code, which then
validates the legs and runs the nine-rule risk gate.

That indirection is the whole safety argument. The model cannot place an order
because no tool that places orders was ever in its list — the capability was
never handed over, so there is no prompt injection or hallucination that
reaches it. `propose_spread` is a request, and requests get refused.

The `rationale` field is required and is not decorative. It is what gets
journalled and rendered on the dashboard, and it is the evidence that a model
reasoned rather than pattern-matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

PROPOSE_SPREAD = "propose_spread"

# Kept deliberately close to the OpenAI function-calling shape: OpenRouter and
# Groq take it as-is, and agent.llm._strip_schema reduces it for Gemini.
PROPOSE_SPREAD_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROPOSE_SPREAD,
        "description": (
            "Propose one bull put credit spread for independent risk review. "
            "This does NOT place an order. A deterministic risk gate evaluates the "
            "proposal against nine rules and may reject it or reduce its size. "
            "Call this at most once, only after you have examined the account, the "
            "existing positions and the option chain. If conditions do not support a "
            "trade, do not call this tool — say so in plain text instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "underlying": {
                    "type": "string",
                    "description": "Underlying ETF symbol, e.g. SPY.",
                },
                "short_strike": {
                    "type": "number",
                    "description": (
                        "Strike of the put you are SELLING. Must be the higher of the "
                        "two strikes and closer to the money."
                    ),
                },
                "long_strike": {
                    "type": "number",
                    "description": (
                        "Strike of the put you are BUYING as protection. Must be lower "
                        "than short_strike. The difference between the two strikes is "
                        "the spread width in DOLLARS."
                    ),
                },
                "expiry": {
                    "type": "string",
                    "description": "Expiration date of both legs, as YYYY-MM-DD.",
                },
                "contracts_requested": {
                    "type": "integer",
                    "description": (
                        "How many spreads to open. The risk gate may approve fewer. "
                        "Must be at least 1."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "Why this trade, citing the specific numbers you observed: IV "
                        "rank, the delta of the short strike, the net credit, days to "
                        "expiry, and current portfolio exposure. A rationale with no "
                        "numbers in it is a bad rationale."
                    ),
                },
            },
            "required": [
                "underlying",
                "short_strike",
                "long_strike",
                "expiry",
                "contracts_requested",
                "rationale",
            ],
        },
    },
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Proposal:
    """A well-formed proposal from the model."""

    underlying: str
    short_strike: float
    long_strike: float
    expiry: str
    contracts_requested: int
    rationale: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> float:
        """Spread width in dollars."""
        return round(self.short_strike - self.long_strike, 2)

    def dte(self, today: date | None = None) -> int:
        return (date.fromisoformat(self.expiry) - (today or date.today())).days

    def to_journal(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "short_strike": self.short_strike,
            "long_strike": self.long_strike,
            "width": self.width,
            "expiry": self.expiry,
            "contracts_requested": self.contracts_requested,
            "rationale": self.rationale,
        }


class MalformedProposal(ValueError):
    """The model called `propose_spread` with arguments that cannot be acted on.

    A normal model failure, not an exceptional one: the orchestrator journals it
    as `malformed_proposal` and moves on to the next underlying.
    """


def parse_proposal(arguments: dict[str, Any]) -> Proposal:
    """Validate the model's arguments into a :class:`Proposal`.

    This is schema-level validation only — that the fields are present, of the
    right type, and internally consistent. Whether the contracts actually exist
    and are quotable is a separate check the orchestrator runs before the gate,
    and whether the trade is *allowed* is the risk gate's business alone. Nothing
    here may approve, size, or veto a trade.
    """
    if not isinstance(arguments, dict):
        raise MalformedProposal(f"expected an object of arguments, got {type(arguments).__name__}")

    missing = [
        key for key in PROPOSE_SPREAD_SCHEMA["function"]["parameters"]["required"]
        if arguments.get(key) in (None, "")
    ]
    if missing:
        raise MalformedProposal(f"missing required field(s): {', '.join(missing)}")

    underlying = str(arguments["underlying"]).strip().upper()
    if not underlying.isalpha():
        raise MalformedProposal(f"underlying {underlying!r} is not a plain ticker symbol")

    try:
        short_strike = float(arguments["short_strike"])
        long_strike = float(arguments["long_strike"])
    except (TypeError, ValueError) as exc:
        raise MalformedProposal(f"strikes must be numbers: {exc}") from exc

    if short_strike <= 0 or long_strike <= 0:
        raise MalformedProposal("strikes must be positive")

    # A bull put spread sells the higher strike and buys the lower one. Inverted,
    # this is a debit spread with a different risk profile entirely — it must be
    # rejected here rather than priced as if it were the intended trade.
    if long_strike >= short_strike:
        raise MalformedProposal(
            f"long_strike ({long_strike}) must be BELOW short_strike ({short_strike}) "
            "for a bull put credit spread"
        )

    expiry = str(arguments["expiry"]).strip()
    if not _ISO_DATE.match(expiry):
        raise MalformedProposal(f"expiry {expiry!r} is not in YYYY-MM-DD form")
    try:
        date.fromisoformat(expiry)
    except ValueError as exc:
        raise MalformedProposal(f"expiry {expiry!r} is not a real date") from exc

    try:
        contracts = int(arguments["contracts_requested"])
    except (TypeError, ValueError) as exc:
        raise MalformedProposal(f"contracts_requested must be an integer: {exc}") from exc
    if contracts < 1:
        raise MalformedProposal(f"contracts_requested must be at least 1, got {contracts}")

    rationale = str(arguments["rationale"]).strip()
    if not rationale:
        raise MalformedProposal("rationale is empty")

    return Proposal(
        underlying=underlying,
        short_strike=short_strike,
        long_strike=long_strike,
        expiry=expiry,
        contracts_requested=contracts,
        rationale=rationale,
        raw=dict(arguments),
    )


def rationale_cites_numbers(rationale: str) -> bool:
    """Whether the rationale contains any figures at all.

    Not a gate — a model is allowed to write a poor rationale, and suppressing
    that would hide exactly the signal worth seeing. The dashboard uses this to
    mark a rationale as thin, so a run full of numberless hand-waving is visible
    rather than buried.
    """
    return bool(re.search(r"\d", rationale or ""))
