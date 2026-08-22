"""Validation of `propose_spread` arguments.

Schema-level only. Whether the contracts exist and are quotable is the
orchestrator's pre-gate check; whether the trade is *allowed* belongs to the
risk gate alone. Nothing here may approve, size, or veto a trade — a validator
that starts making risk decisions is a second, undocumented gate.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.tools import (
    PROPOSE_SPREAD,
    PROPOSE_SPREAD_SCHEMA,
    MalformedProposal,
    parse_proposal,
    rationale_cites_numbers,
)

VALID = {
    "underlying": "SPY",
    "short_strike": 752.0,
    "long_strike": 747.0,
    "expiry": "2026-08-31",
    "contracts_requested": 4,
    "rationale": (
        "IV rank 42, short leg delta -0.179, net credit $53 per spread on a $5 width, "
        "9 DTE, no existing SPY exposure."
    ),
}


def test_a_valid_proposal_parses():
    proposal = parse_proposal(VALID)
    assert proposal.underlying == "SPY"
    assert proposal.short_strike == 752.0
    assert proposal.long_strike == 747.0
    assert proposal.contracts_requested == 4
    assert proposal.width == 5.0


def test_width_is_in_dollars_not_strikes():
    """SPY has $1.00 strike spacing, so a $5 width is five strikes apart.

    Conflating the two is how a model ends up proposing a $5 spread it believes
    is five times wider.
    """
    assert parse_proposal({**VALID, "short_strike": 752, "long_strike": 742}).width == 10.0


def test_dte_is_computed_from_the_expiry():
    assert parse_proposal(VALID).dte(today=date(2026, 8, 22)) == 9


def test_underlying_is_normalised():
    assert parse_proposal({**VALID, "underlying": " spy "}).underlying == "SPY"


# ------------------------------------------------------------ the inversion


def test_an_inverted_spread_is_rejected():
    """Buying the higher strike is a debit spread — a different trade entirely.

    Pricing it as though it were the intended credit spread would submit a trade
    that pays out instead of collecting.
    """
    with pytest.raises(MalformedProposal, match="must be BELOW"):
        parse_proposal({**VALID, "short_strike": 747.0, "long_strike": 752.0})


def test_identical_strikes_are_rejected():
    with pytest.raises(MalformedProposal, match="must be BELOW"):
        parse_proposal({**VALID, "short_strike": 750.0, "long_strike": 750.0})


# ------------------------------------------------------------- missing input


@pytest.mark.parametrize("field", sorted(PROPOSE_SPREAD_SCHEMA["function"]["parameters"]["required"]))
def test_every_required_field_is_required(field):
    args = {k: v for k, v in VALID.items() if k != field}
    with pytest.raises(MalformedProposal, match="missing required"):
        parse_proposal(args)


@pytest.mark.parametrize("field", ["underlying", "expiry", "rationale"])
def test_an_empty_string_counts_as_missing(field):
    with pytest.raises(MalformedProposal, match="missing required"):
        parse_proposal({**VALID, field: ""})


def test_a_whitespace_only_rationale_is_rejected():
    """A blank rationale defeats the point of requiring one."""
    with pytest.raises(MalformedProposal, match="rationale is empty"):
        parse_proposal({**VALID, "rationale": "   "})


# ------------------------------------------------------------- malformed input


@pytest.mark.parametrize("bad", ["not-a-date", "31-08-2026", "2026/08/31", "2026-8-31"])
def test_a_non_iso_expiry_is_rejected(bad):
    with pytest.raises(MalformedProposal, match="YYYY-MM-DD"):
        parse_proposal({**VALID, "expiry": bad})


def test_an_impossible_date_is_rejected():
    with pytest.raises(MalformedProposal, match="not a real date"):
        parse_proposal({**VALID, "expiry": "2026-02-31"})


@pytest.mark.parametrize("bad", ["abc", None, [1]])
def test_non_numeric_strikes_are_rejected(bad):
    with pytest.raises(MalformedProposal):
        parse_proposal({**VALID, "short_strike": bad})


@pytest.mark.parametrize("bad", [0, -1, -752.0])
def test_non_positive_strikes_are_rejected(bad):
    with pytest.raises(MalformedProposal):
        parse_proposal({**VALID, "short_strike": bad, "long_strike": bad - 5})


@pytest.mark.parametrize("bad", [0, -3])
def test_a_non_positive_contract_count_is_rejected(bad):
    with pytest.raises(MalformedProposal, match="at least 1"):
        parse_proposal({**VALID, "contracts_requested": bad})


def test_a_non_integer_contract_count_is_rejected():
    with pytest.raises(MalformedProposal, match="integer"):
        parse_proposal({**VALID, "contracts_requested": "four"})


def test_a_ticker_with_digits_is_rejected():
    with pytest.raises(MalformedProposal, match="plain ticker"):
        parse_proposal({**VALID, "underlying": "SPY260831P00752000"})


def test_non_dict_arguments_are_rejected():
    with pytest.raises(MalformedProposal):
        parse_proposal("SPY 752/747")  # type: ignore[arg-type]


# ------------------------------------------------------- validator boundaries


def test_the_validator_does_not_enforce_risk_rules():
    """Delta, DTE, credit and size limits belong to the risk gate, not here.

    A 400-contract request 200 days out is well outside the strategy, but it is
    well-formed. It must reach the gate and be rejected there with a rule number
    and an observed-vs-limit, not be silently dropped by a validator.
    """
    proposal = parse_proposal({
        **VALID, "contracts_requested": 400, "expiry": "2027-06-18", "short_strike": 400.0,
        "long_strike": 100.0,
    })
    assert proposal.contracts_requested == 400
    assert proposal.width == 300.0


# ------------------------------------------------------------------ rationale


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("IV rank 42, delta -0.18, credit $53.", True),
        ("Conditions look favourable and the setup seems attractive.", False),
        ("", False),
    ],
)
def test_thin_rationales_are_detectable_but_not_blocked(text, expected):
    """Surfaced on the dashboard, never used to reject.

    Suppressing a numberless rationale would hide exactly the signal worth
    seeing: a model producing confident-sounding text with no evidence in it.
    """
    assert rationale_cites_numbers(text) is expected


# --------------------------------------------------------------------- schema


def test_the_schema_advertises_that_it_does_not_place_an_order():
    """The description is the model's only cue that this is a request, not an act."""
    description = PROPOSE_SPREAD_SCHEMA["function"]["description"]
    assert "does NOT place an order" in description
    assert "risk gate" in description


def test_the_schema_gives_the_model_permission_to_decline():
    """Without this, models get sycophantic and propose to please rather than to reason."""
    assert "do not call this tool" in PROPOSE_SPREAD_SCHEMA["function"]["description"]


def test_the_schema_name_matches_the_constant():
    assert PROPOSE_SPREAD_SCHEMA["function"]["name"] == PROPOSE_SPREAD
