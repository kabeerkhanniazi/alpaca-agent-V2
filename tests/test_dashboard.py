"""Dashboard smoke tests.

Runs the Streamlit script end to end and asserts it renders without raising.
A broken dashboard is a submission-day problem — this catches it here instead.

Marked ``integration`` because it hits the live Alpaca API for account state.
Run the rest of the suite with ``-m "not integration"`` to stay fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

REPO_ROOT = Path(__file__).resolve().parent.parent


def render():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(REPO_ROOT / "streamlit_app.py"), default_timeout=300)
    app.run()
    return app


def markup(app) -> str:
    return "\n".join(m.value for m in app.markdown)


@pytest.mark.integration
def test_dashboard_renders_without_error():
    app = render()
    assert not app.exception, [str(e.value) for e in app.exception]


@pytest.mark.integration
def test_every_panel_survives_the_restyle():
    """The restyle must not have dropped a single panel."""
    blob = markup(render())
    for anchor in ("portfolio", "risk-gate", "performance", "market", "journal"):
        assert f'id="{anchor}"' in blob, f"{anchor} section missing"


@pytest.mark.integration
def test_all_four_journal_tabs_survive():
    assert len(render().tabs) == 4


@pytest.mark.integration
def test_greeks_and_headroom_bars_survive():
    """The Greeks row and both headroom bars must survive the restyle.

    These panels need live account data, so the assertion is written against
    both legitimate outcomes: the panels render when Alpaca answers, and the
    documented placeholder renders when it does not. Anything else — a blank
    region, a traceback — fails.

    Written this way deliberately: an earlier version asserted the populated
    case only and went red during an Alpaca outage, which is a bad test. A
    third-party blip should not turn the suite red, but a dropped panel must.
    """
    blob = markup(render())

    if "Live data unavailable" in blob:
        # Degraded path: the placeholder must be there instead, and the
        # journal-derived panels must still have rendered below it.
        assert 'id="risk-gate"' in blob
        assert 'id="performance"' in blob
        return

    for label in ("Net delta", "Theta / day", "Vega", "Gamma"):
        assert label in blob, f"Greeks row lost {label}"
    assert "Portfolio delta · Rule 3" in blob
    assert "Daily drawdown · Rule 8" in blob


@pytest.mark.integration
def test_an_alpaca_outage_does_not_blank_the_page():
    """Whatever Alpaca is doing, the journal-derived panels must render."""
    blob = markup(render())
    assert 'id="journal"' in blob
    assert 'oa-rule-name">R1 ·' in blob


@pytest.mark.integration
def test_performance_metrics_survive():
    blob = markup(render())
    for label in ("Win rate", "Realized P&amp;L", "Positions closed",
                  "Orders filled", "Avg credit", "Gate approval rate"):
        assert label in blob, f"Performance panel lost {label}"


@pytest.mark.integration
def test_risk_gate_lists_all_nine_rules():
    blob = markup(render())
    for n in range(1, 10):
        assert f'oa-rule-name">R{n} ·' in blob, f"rule R{n} missing from the gate panel"


@pytest.mark.integration
def test_no_control_can_reach_the_broker():
    """The whole page must expose exactly one button: Refresh.

    This dashboard is deployed publicly. The mockup's EXECUTE LIVE and KILL
    SWITCH are rendered as status spans precisely so a stranger cannot click
    them; if a real button for either ever appears, this fails.
    """
    app = render()
    labels = [b.label for b in app.button]
    assert labels == ["Refresh now"], f"unexpected controls on the page: {labels}"


@pytest.mark.integration
def test_status_pills_are_spans_not_buttons():
    blob = markup(render())
    assert "MODE:" in blob and "KILL-SWITCH:" in blob
    # They must be pill spans, and must not appear as clickable elements.
    assert "oa-pill" in blob
    assert "<button" not in blob


@pytest.mark.integration
def test_no_mockup_placeholder_survives():
    """None of the mockup's hardcoded chrome may reach the page."""
    blob = markup(render())
    for literal in ("NEURAL COMMAND", "Neural Link", "EXECUTE LIVE", "Logout",
                    "Mainframe", "Liquidity", "2d 3h 15m"):
        assert literal not in blob, f"mockup literal leaked into the render: {literal}"


@pytest.mark.integration
def test_no_dead_links():
    """Every anchor href must resolve to an id that exists on the page."""
    import re

    blob = markup(render())
    ids = set(re.findall(r'id="([\w-]+)"', blob))
    for href in re.findall(r'href="#([\w-]+)"', blob):
        assert href in ids, f"dead link: #{href} has no matching element"


@pytest.mark.integration
def test_stylesheet_carries_no_script_or_link_tags():
    """Streamlit strips <script>; <link> would be a silent network dependency."""
    blob = markup(render())
    assert "<script" not in blob.lower()
    assert "<link" not in blob.lower()


# ------------------------------------------------- agent reasoning panel

@pytest.mark.integration
def test_the_agent_reasoning_panel_renders():
    """This panel is the evidence an AI agent exists; everything else could be a script."""
    blob = markup(render())
    assert 'id="agent-reasoning"' in blob


@pytest.mark.integration
def test_the_panel_shows_the_gate_verdict_beside_the_reasoning():
    """Reasoning without the verdict beside it is only half the story."""
    app = render()
    blob = markup(app)
    assert "The model's reasoning" in blob
    assert "The gate's verdict" in blob


def test_every_no_proposal_outcome_has_a_distinct_label_and_tone():
    """Two healthy outcomes and three defects must not render identically.

    Rendering them the same way hides the difference at exactly the moment it
    matters — a five-day run where the agent quietly stopped proposing.
    """
    import re

    source = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    block = re.search(r"OUTCOME_STYLE = \{(.*?)\n\}", source, re.S).group(1)

    labels = dict(re.findall(r'"(\w+)": \("([^"]+)", "(\w+)"\)', block)[:0]) or {}
    pairs = re.findall(r'"(\w+)": \("([^"]+)", "(\w+)"\)', block)
    assert len(pairs) == 5, "all five agent outcomes must have a style"

    by_event = {event: (label, tone) for event, label, tone in pairs}
    assert by_event["agent_proposal"][1] == "ok"
    assert by_event["no_proposal_declined"][1] == "neutral", "declining is healthy"
    # The three defects must not read as neutral or healthy.
    for defect in ("no_proposal_turn_limit", "malformed_proposal", "invalid_proposal"):
        assert by_event[defect][1] in ("warn", "bad"), f"{defect} must read as a defect"

    assert len({label for label, _ in by_event.values()}) == 5, "labels must be distinct"


def test_a_thin_rationale_is_flagged_but_never_suppressed():
    """A model producing confident text with no numbers is exactly what to surface."""
    source = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    assert "cites no numbers" in source
    # It must still be rendered: the flag is a caption beside it, not a filter.
    assert "rationale else" in source or "T.esc(rationale)" in source


def test_the_dashboard_renders_with_credentials_stubbed_out(monkeypatch):
    """A public deployment without Alpaca keys must render, not error.

    This is not hypothetical: the Streamlit Cloud instance reads the journal
    from a git branch and may have no broker credentials at all. Every
    journal-derived panel has to work exactly as normal.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")

    app = render()
    assert not app.exception, [str(e.value) for e in app.exception]

    blob = markup(app)
    for anchor in ("portfolio", "risk-gate", "performance", "market",
                   "journal", "agent-reasoning"):
        assert f'id="{anchor}"' in blob, f"{anchor} vanished without credentials"


def test_the_live_accessors_raise_rather_than_returning_none(monkeypatch):
    """`safe()` turns an exception into the 'unavailable' card.

    Returning None instead would read as "no error, no data" and render an empty
    panel with no explanation of why.
    """
    source = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    assert "_live_or_raise" in source
    assert 'raise RuntimeError(state["unavailable"])' in source
