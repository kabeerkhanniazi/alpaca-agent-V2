"""Unit tests for the theme module's pure HTML builders and stylesheet.

None of these need a Streamlit runtime or a network — the builders are plain
functions over plain data, which is why they live outside streamlit_app.py.
"""

from __future__ import annotations

import re

import pytest

from agent import dashboard_theme as T


# ------------------------------------------------------------ stylesheet

def test_stylesheet_has_no_script_tag():
    """Streamlit strips <script>; relying on one would silently break."""
    assert "<script" not in T.CSS.lower()


def test_stylesheet_has_no_link_tag():
    """Fonts load via @import inside the style block, not a <link>."""
    assert "<link" not in T.CSS.lower()


def test_fonts_load_via_import():
    assert "@import url(" in T.CSS
    for family in ("Sora", "Inter", "Space+Grotesk", "JetBrains+Mono"):
        assert family in T.CSS, f"{family} not imported"


def test_outline_is_never_a_text_colour():
    """`outline` scores 2.46:1 on the card background — it fails AA.

    It is a border-only token. A `color: var(--outline)` declaration anywhere
    would put unreadable text on the page, which is one of the two defects the
    mockup screenshot was called out for.
    """
    declarations = re.findall(r"(?<!-)\bcolor:\s*var\(--outline\)", T.CSS)
    assert declarations == [], "outline used as a text colour"


def test_figures_are_allowed_to_shrink():
    """The fix for the buying-power figure clipping out of its card."""
    figure = T.CSS[T.CSS.index(".oa-figure"):T.CSS.index(".oa-section-title")]
    assert "clamp(" in figure
    assert "min-width: 0" in figure
    assert "overflow-wrap: anywhere" in figure


def test_wide_content_scrolls_inside_its_own_box():
    """The page body must never scroll sideways."""
    assert ".oa-table-scroll { overflow-x: auto; }" in T.CSS
    assert "overflow-x: hidden" in T.CSS


def test_a_narrow_breakpoint_exists():
    assert "@media (max-width: 640px)" in T.CSS


def test_motion_can_be_reduced():
    assert "prefers-reduced-motion" in T.CSS


def test_glow_is_rationed():
    """Glow on every figure destroys legibility; only a few classes define it."""
    glow_classes = re.findall(r"\.oa-glow-\w+", T.CSS)
    assert len(set(glow_classes)) <= 3


# ------------------------------------------------------------- escaping

def test_interpolated_values_are_escaped():
    """Journal text is not authored here — a stray bracket must not break layout."""
    out = T.metric_card("Label", '<img src=x onerror="alert(1)">')
    assert "<img" not in out
    assert "&lt;img" in out


def test_terminal_lines_escape_their_text():
    out = T.terminal_line("12:00:00", "analysis", "<b>not bold</b>")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out


# ----------------------------------------------------------- P&L colour

@pytest.mark.parametrize("value,expected", [
    (120.0, ("secondary", "t-secondary")),
    (-80.0, ("error", "t-error")),
    (0.0, ("neutral", "t-variant")),
])
def test_pnl_is_colour_coded_by_meaning(value, expected):
    assert T.pnl_classes(value) == expected


# --------------------------------------------------------- regime pills

@pytest.mark.parametrize("regime,tone", [
    ("HIGH_IV", "tertiary"),   # amber — premium is rich
    ("MID_IV", "secondary"),   # cyan
    ("LOW_IV", "primary"),     # pink — too cheap to sell
])
def test_regime_pill_colour(regime, tone):
    assert f"oa-pill-{tone}" in T.regime_pill(regime)


def test_unknown_regime_falls_back_to_neutral():
    assert "oa-pill-neutral" in T.regime_pill("SOMETHING_ELSE")


# -------------------------------------------------------- headroom bars

def test_headroom_bar_clamps_above_the_limit():
    """A breached limit renders a full bar, never one wider than its track."""
    out = T.headroom_bar("Delta", 999, "over", 3.4)
    width = float(re.search(r"width:([\d.]+)%", out).group(1))
    assert width == 100.0


def test_headroom_bar_clamps_below_zero():
    out = T.headroom_bar("Delta", 0, "none", -1.0)
    assert "width:0.0%" in out


@pytest.mark.parametrize("pct,cls", [(0.2, "oa-bar-ok"), (0.7, "oa-bar-warn"), (0.95, "oa-bar-hot")])
def test_headroom_bar_warms_as_it_fills(pct, cls):
    assert cls in T.headroom_bar("x", 1, "y", pct)


# --------------------------------------------------------- status pills

def test_status_pill_is_a_span_not_a_button():
    """Nothing on a public dashboard may look clickable if it touches trading."""
    out = T.status_pill("MODE: LIVE", "primary")
    assert out.startswith("<span")
    assert "<button" not in out
    assert "onclick" not in out


# ------------------------------------------------------------ rejection

def test_rejection_card_shows_limit_and_actual():
    out = T.rejection_card("R4", "Minimum credit", "SPY 753/748", "too thin", "63.0", "500.0")
    assert "R4" in out
    assert "500.0" in out and "63.0" in out
    assert "oa-breach" in out


def test_rejection_badge_leads_with_the_rule_number():
    out = T.rejection_card("R8", "Kill-switch", "SPY", "tripped", "0.08", "0.05")
    assert out.index("R8") < out.index("Kill-switch")


# ----------------------------------------------------------- rule rows

@pytest.mark.parametrize("state,cls", [
    ("pass", "oa-rule-pass"), ("fail", "oa-rule-fail"), ("idle", "oa-rule-idle"),
])
def test_rule_row_state(state, cls):
    assert cls in T.rule_row("R1", "Delta", "≤ 0.20", state)


def test_an_unevaluated_rule_does_not_claim_to_pass():
    """Before any spread is seen, rules show a threshold and a neutral state."""
    out = T.rule_row("R1", "Delta", "≤ 0.20", "idle")
    assert "oa-rule-idle" in out
    assert "oa-rule-pass" not in out


def test_rule_row_shows_observed_when_available():
    assert "observed 0.199" in T.rule_row("R1", "Delta", "≤ 0.20", "pass", "0.199")


# --------------------------------------------------------------- banner

def test_banner_renders_open_and_closed_differently():
    closed = T.market_banner(False, [("2", "d")], "Opens in", "next session")
    opened = T.market_banner(True, [("2", "h")], "Closes in", "session ends")
    assert "MARKET CLOSED" in closed and "is-open" not in closed
    assert "MARKET OPEN" in opened and "is-open" in opened


def test_banner_handles_a_missing_countdown():
    out = T.market_banner(False, [], "Opens in", "unknown")
    assert "—" in out


# ---------------------------------------------------------------- icons

def test_icons_are_inline_svg_not_a_font():
    """An icon font is a CDN dependency that leaves ligature text when it fails."""
    out = T.icon("bank")
    assert out.startswith("<svg")
    assert "material-symbols" not in out.lower()
