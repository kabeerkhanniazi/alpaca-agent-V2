"""Visual language for the dashboard: tokens, stylesheet, and HTML builders.

Ported by hand from the Tailwind mockup in ``docs/design/alpaca.html``. Tailwind
cannot be used directly — it ships as a ``<script>`` tag and Streamlit strips
those — so every utility class the mockup relied on is written out as plain CSS
here and injected once via ``st.markdown(..., unsafe_allow_html=True)``.

Two rules govern everything in this file:

**``outline`` is a border-only token.** Measured against the composited
glass-card background (``#12121e``) it scores 2.46:1, which fails WCAG AA and
even AA-large. The mockup uses it for the source column and the P&L percentage;
both are ``on-surface-variant`` here instead, which scores 6.73:1. No text
anywhere uses ``outline``.

**Neon glow is rationed.** ``text-shadow`` on every figure destroys legibility
against a dark ground. It appears on exactly two things: the market banner
headline and the net-liquidation figure.

Everything below is a pure function returning an HTML string, so it can be
unit-tested without a Streamlit runtime.
"""

from __future__ import annotations

import html
from typing import Any

TOKENS = {
    "background": "#0a0a12",
    "surface": "#0f0f1a",
    "surface_container": "#141422",
    "surface_container_high": "#1e1e30",
    "primary": "#ff2d78",
    "secondary": "#00ffcc",
    "tertiary": "#ffe04a",
    "error": "#ff4444",
    "on_surface": "#e8e0f0",
    "on_surface_variant": "#a098b0",
    "outline": "#5a5068",
    "outline_variant": "#302840",
    "terminal": "#050508",
}

# Regime -> (token name, background alpha). Amber for rich premium, cyan for
# middling, pink for too cheap to sell — matching the mockup.
REGIME_COLORS = {
    "HIGH_IV": "tertiary",
    "MID_IV": "secondary",
    "LOW_IV": "primary",
    "UNKNOWN": "on_surface_variant",
}

# Journal event type -> terminal line colour.
EVENT_COLORS = {
    "trade_approved": "secondary",
    "trade_rejected": "error",
    "order_filled": "secondary",
    "order_submitted": "secondary",
    "order_failed": "error",
    "order_dry_run": "on_surface_variant",
    "position_exit": "tertiary",
    "cycle_skipped": "on_surface_variant",
    "cycle_summary": "on_surface_variant",
    "analysis": "on_surface_variant",
    "spread_candidates": "on_surface_variant",
    "error": "error",
}


def esc(value: Any) -> str:
    """Escape a value for interpolation into HTML.

    Journal entries carry broker messages and rule text that are not authored
    here; interpolating them raw would let a stray angle bracket break the
    layout.
    """
    return html.escape(str(value), quote=True)


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=Inter:wght@400;500;600&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg: #0a0a12;
  --surface: #0f0f1a;
  --surface-container: #141422;
  --surface-container-high: #1e1e30;
  --primary: #ff2d78;
  --secondary: #00ffcc;
  --tertiary: #ffe04a;
  --error: #ff4444;
  --on-surface: #e8e0f0;
  --on-surface-variant: #a098b0;
  --outline: #5a5068;
  --outline-variant: #302840;
  --terminal: #050508;
  --font-display: 'Sora', system-ui, sans-serif;
  --font-body: 'Inter', system-ui, sans-serif;
  --font-label: 'Space Grotesk', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, 'Courier New', monospace;
}

/* The cyber-grid ground. Applied to the app shell rather than <body> because
   Streamlit paints its own background over the document body. */
.stApp {
  background-color: var(--bg);
  background-image:
    linear-gradient(rgba(0, 255, 204, 0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0, 255, 204, 0.03) 1px, transparent 1px);
  background-size: 30px 30px;
}

html, body, .stApp, [data-testid="stAppViewContainer"] {
  font-family: var(--font-body);
  color: var(--on-surface);
}

/* Streamlit's default top padding wastes a screen of vertical space. */
.block-container { padding-top: 2.2rem !important; max-width: 1500px; }

/* Never let the page scroll sideways, at any width. */
.stApp, .block-container { overflow-x: hidden; }

h1, h2, h3, h4 { font-family: var(--font-display); }

/* ---------------------------------------------------------- primitives */

.oa-glass {
  background: rgba(30, 30, 48, 0.4);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(90, 80, 104, 0.3);
  border-radius: 12px;
}

.oa-label {
  font-family: var(--font-label);
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  /* on-surface-variant: 6.73:1 on the glass card. Never `outline` (2.46:1). */
  color: var(--on-surface-variant);
}

/* Figures must shrink rather than overflow. This is the fix for the buying
   power value clipping out of its card in the mockup screenshot. */
.oa-figure {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: clamp(1.05rem, 2.1vw, 1.65rem);
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
  min-width: 0;
  margin: 0;
}

.oa-section-title {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 1.1rem;
  letter-spacing: 0.02em;
  color: var(--on-surface);
  margin: 0;
}

/* --------------------------------------------------------- metric cards */

.oa-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 14px;
  margin-bottom: 6px;
}

.oa-card {
  background: rgba(30, 30, 48, 0.4);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(90, 80, 104, 0.3);
  border-left: 2px solid var(--outline);
  border-radius: 10px;
  padding: 16px 18px;
  min-width: 0;
  transition: transform 0.2s ease, border-color 0.2s ease;
}
.oa-card:hover { transform: translateY(-3px); }

.oa-card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 10px;
}
.oa-card-head .oa-label { min-width: 0; overflow-wrap: anywhere; }
.oa-card-icon { flex: 0 0 auto; opacity: 0.85; }

.oa-sub {
  font-family: var(--font-label);
  font-size: 0.72rem;
  color: var(--on-surface-variant);
  margin-top: 5px;
  display: block;
}

.oa-accent-primary { border-left-color: var(--primary); }
.oa-accent-secondary { border-left-color: var(--secondary); }
.oa-accent-tertiary { border-left-color: var(--tertiary); }
.oa-accent-error { border-left-color: var(--error); }
.oa-accent-neutral { border-left-color: var(--outline); }

.t-primary { color: var(--primary); }
.t-secondary { color: var(--secondary); }
.t-tertiary { color: var(--tertiary); }
.t-error { color: var(--error); }
.t-on-surface { color: var(--on-surface); }
.t-variant { color: var(--on-surface-variant); }

/* Rationed glow: banner headline and one hero figure only. */
.oa-glow-primary { text-shadow: 0 0 10px rgba(255, 45, 120, 0.45); }
.oa-glow-error { text-shadow: 0 0 10px rgba(255, 68, 68, 0.5); }
.oa-glow-secondary { text-shadow: 0 0 10px rgba(0, 255, 204, 0.45); }

/* -------------------------------------------------------------- banner */

.oa-banner {
  position: relative;
  border-radius: 14px;
  padding: 22px 26px;
  margin-bottom: 18px;
  background: linear-gradient(90deg, rgba(255, 45, 120, 0.08), rgba(10, 10, 18, 0.2));
  border: 1px solid rgba(255, 45, 120, 0.32);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.oa-banner.is-open {
  background: linear-gradient(90deg, rgba(0, 255, 204, 0.08), rgba(10, 10, 18, 0.2));
  border-color: rgba(0, 255, 204, 0.32);
}

.oa-banner-left { display: flex; align-items: center; gap: 18px; min-width: 0; }

.oa-orb {
  width: 56px; height: 56px;
  flex: 0 0 auto;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  border: 2px solid var(--error);
  background: rgba(255, 68, 68, 0.14);
  animation: oa-pulse 2s infinite;
}
.oa-banner.is-open .oa-orb {
  border-color: var(--secondary);
  background: rgba(0, 255, 204, 0.14);
  animation: oa-pulse-open 2s infinite;
}

@keyframes oa-pulse {
  0%, 100% { box-shadow: 0 0 20px rgba(255, 68, 68, 0.45); opacity: 1; }
  50%      { box-shadow: 0 0 8px rgba(255, 68, 68, 0.2); opacity: 0.82; }
}
@keyframes oa-pulse-open {
  0%, 100% { box-shadow: 0 0 20px rgba(0, 255, 204, 0.45); opacity: 1; }
  50%      { box-shadow: 0 0 8px rgba(0, 255, 204, 0.2); opacity: 0.82; }
}
@media (prefers-reduced-motion: reduce) {
  .oa-orb, .oa-cursor { animation: none !important; }
}

.oa-banner-title {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: clamp(1.5rem, 4vw, 2.1rem);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin: 0;
  overflow-wrap: anywhere;
}
.oa-banner-sub {
  font-family: var(--font-body);
  font-size: 0.85rem;
  color: var(--on-surface-variant);
  margin: 6px 0 0 0;
}

.oa-countdown { text-align: right; min-width: 0; }
.oa-countdown-figure {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: clamp(1.6rem, 4vw, 2.3rem);
  color: var(--on-surface);
  display: flex; gap: 12px; justify-content: flex-end;
  align-items: baseline;
  font-variant-numeric: tabular-nums;
  flex-wrap: wrap;
}
.oa-countdown-figure .u {
  font-size: 0.55em;
  color: var(--on-surface-variant);
  margin-left: 2px;
}

/* --------------------------------------------------------------- pills */

.oa-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 999px;
  font-family: var(--font-label);
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  white-space: nowrap;
  /* Status only. Not a button, not focusable, nothing to click. */
  cursor: default;
  user-select: none;
}
.oa-pill-secondary { background: rgba(0, 255, 204, 0.12); border: 1px solid rgba(0, 255, 204, 0.5); color: var(--secondary); }
.oa-pill-primary   { background: rgba(255, 45, 120, 0.12); border: 1px solid rgba(255, 45, 120, 0.5); color: var(--primary); }
.oa-pill-tertiary  { background: rgba(255, 224, 74, 0.12); border: 1px solid rgba(255, 224, 74, 0.5); color: var(--tertiary); }
.oa-pill-error     { background: rgba(255, 68, 68, 0.12); border: 1px solid rgba(255, 68, 68, 0.5); color: var(--error); }
.oa-pill-neutral   { background: rgba(160, 152, 176, 0.1); border: 1px solid rgba(90, 80, 104, 0.6); color: var(--on-surface-variant); }

.oa-pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 10px 0; }

/* -------------------------------------------------------- headroom bars */

.oa-bar-wrap { margin-bottom: 14px; min-width: 0; }
.oa-bar-head {
  display: flex; justify-content: space-between; gap: 10px;
  margin-bottom: 6px; flex-wrap: wrap;
}
.oa-bar-value {
  font-family: var(--font-label);
  font-size: 0.72rem;
  color: var(--on-surface);
  font-variant-numeric: tabular-nums;
}
.oa-bar-track {
  height: 7px;
  border-radius: 999px;
  background: var(--surface-container-high);
  overflow: hidden;
  border: 1px solid rgba(90, 80, 104, 0.35);
}
.oa-bar-fill { height: 100%; border-radius: 999px; transition: width 0.4s ease; }
.oa-bar-ok   { background: linear-gradient(90deg, rgba(0,255,204,0.55), var(--secondary)); }
.oa-bar-warn { background: linear-gradient(90deg, rgba(255,224,74,0.55), var(--tertiary)); }
.oa-bar-hot  { background: linear-gradient(90deg, rgba(255,68,68,0.55), var(--error)); }

/* -------------------------------------------------------------- tables */

.oa-table-wrap {
  border: 1px solid var(--outline-variant);
  border-radius: 12px;
  overflow: hidden;
  margin-bottom: 6px;
}
.oa-table-head {
  background: var(--surface-container-high);
  padding: 13px 18px;
  border-bottom: 1px solid rgba(90, 80, 104, 0.4);
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
}
/* Wide tables scroll inside their own box so the page never does. */
.oa-table-scroll { overflow-x: auto; }
.oa-table { width: 100%; border-collapse: collapse; font-size: 0.84rem; min-width: 520px; }
.oa-table th {
  font-family: var(--font-label);
  font-size: 0.66rem;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--on-surface-variant);
  text-align: left;
  padding: 11px 18px;
  background: rgba(15, 15, 26, 0.5);
  border-bottom: 1px solid rgba(90, 80, 104, 0.3);
  white-space: nowrap;
}
.oa-table td {
  padding: 12px 18px;
  border-bottom: 1px solid rgba(48, 40, 64, 0.5);
  color: var(--on-surface);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.oa-table tbody tr { transition: background 0.15s ease; }
.oa-table tbody tr:hover { background: rgba(30, 30, 48, 0.55); }
.oa-table tbody tr:last-child td { border-bottom: none; }
.oa-table .oa-ticker { font-family: var(--font-display); font-weight: 700; color: var(--secondary); }
/* The mockup renders this column in `outline`, which fails AA. */
.oa-table .oa-muted { color: var(--on-surface-variant); font-family: var(--font-label); font-size: 0.74rem; }

/* ----------------------------------------------------------- risk gate */

.oa-rules { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 10px; }
.oa-rule {
  display: flex; align-items: flex-start; gap: 11px;
  padding: 11px 13px;
  border-radius: 9px;
  background: rgba(20, 20, 34, 0.65);
  border: 1px solid rgba(48, 40, 64, 0.8);
  min-width: 0;
}
.oa-rule-badge {
  flex: 0 0 auto;
  width: 26px; height: 26px;
  border-radius: 7px;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-label); font-size: 0.7rem; font-weight: 700;
}
.oa-rule-pass { background: rgba(0, 255, 204, 0.14); color: var(--secondary); border: 1px solid rgba(0,255,204,0.4); }
.oa-rule-fail { background: rgba(255, 68, 68, 0.14); color: var(--error); border: 1px solid rgba(255,68,68,0.45); }
.oa-rule-idle { background: rgba(160, 152, 176, 0.1); color: var(--on-surface-variant); border: 1px solid rgba(90,80,104,0.5); }
.oa-rule-body { min-width: 0; }
.oa-rule-name {
  font-family: var(--font-body); font-size: 0.82rem; font-weight: 600;
  color: var(--on-surface); overflow-wrap: anywhere;
}
.oa-rule-meta {
  font-family: var(--font-label); font-size: 0.7rem;
  color: var(--on-surface-variant); margin-top: 3px;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere;
}

/* --------------------------------------------------------- rejections */

.oa-reject {
  border: 1px solid rgba(255, 68, 68, 0.35);
  border-left: 3px solid var(--error);
  border-radius: 10px;
  background: rgba(61, 15, 15, 0.22);
  padding: 15px 17px;
  margin-bottom: 12px;
}
.oa-reject-head { display: flex; align-items: center; gap: 13px; flex-wrap: wrap; margin-bottom: 9px; }
.oa-reject-badge {
  font-family: var(--font-display); font-weight: 800;
  font-size: 1.05rem; color: var(--error);
  background: rgba(255, 68, 68, 0.14);
  border: 1px solid rgba(255, 68, 68, 0.45);
  border-radius: 8px; padding: 4px 11px; white-space: nowrap;
}
.oa-reject-title { font-family: var(--font-body); font-weight: 600; color: var(--on-surface); min-width: 0; overflow-wrap: anywhere; }
.oa-breach {
  display: flex; gap: 22px; flex-wrap: wrap;
  padding: 10px 13px; border-radius: 8px;
  background: rgba(10, 10, 18, 0.55);
  border: 1px solid rgba(48, 40, 64, 0.9);
  margin-top: 9px;
}
.oa-breach div { min-width: 0; }
.oa-breach-num {
  font-family: var(--font-display); font-weight: 700; font-size: 1rem;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere;
}
.oa-reject-reason { font-size: 0.84rem; color: var(--on-surface-variant); overflow-wrap: anywhere; }

/* ------------------------------------------------------------ terminal */

.oa-terminal {
  background: var(--terminal);
  border: 1px solid var(--outline-variant);
  border-radius: 10px;
  padding: 15px 17px;
  position: relative;
  overflow: hidden;
}
.oa-terminal::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(0, 255, 204, 0.4), transparent);
}
.oa-term-inner {
  background: var(--bg);
  border: 1px solid rgba(48, 40, 64, 0.75);
  border-radius: 7px;
  padding: 13px 15px;
  font-family: var(--font-mono);
  font-size: 0.76rem;
  line-height: 1.72;
  max-height: 420px;
  overflow-y: auto;
  overflow-x: auto;
}
.oa-term-line { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
.oa-cursor {
  display: inline-block; width: 7px; height: 13px;
  background: var(--secondary); vertical-align: middle;
  animation: oa-blink 1.1s step-end infinite;
}
@keyframes oa-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

/* --------------------------------------------------- streamlit chrome */

/* Tabs restyled to match. If these internals change in a future Streamlit,
   the content still renders — only the skin degrades. */
.stTabs [data-baseweb="tab-list"] {
  gap: 4px;
  border-bottom: 1px solid var(--outline-variant);
  background: transparent;
}
.stTabs [data-baseweb="tab"] {
  font-family: var(--font-label);
  font-size: 0.76rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--on-surface-variant);
  background: transparent;
  padding: 8px 14px;
}
.stTabs [aria-selected="true"] { color: var(--secondary) !important; }
.stTabs [data-baseweb="tab-highlight"] { background-color: var(--secondary); }

section[data-testid="stSidebar"] {
  background: var(--surface);
  border-right: 1px solid rgba(255, 45, 120, 0.22);
}

.stButton > button {
  font-family: var(--font-label);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 0.72rem;
  border: 1px solid rgba(0, 255, 204, 0.45);
  background: rgba(0, 255, 204, 0.08);
  color: var(--secondary);
  border-radius: 8px;
}
.stButton > button:hover {
  border-color: var(--secondary);
  background: rgba(0, 255, 204, 0.16);
  color: var(--secondary);
}

[data-testid="stExpander"] details {
  border: 1px solid var(--outline-variant);
  border-radius: 10px;
  background: rgba(20, 20, 34, 0.5);
}

hr { border-color: var(--outline-variant) !important; }

/* Narrow screens: the countdown moves under the headline rather than
   squeezing it, and cards go single-column. */
@media (max-width: 640px) {
  .oa-banner { flex-direction: column; align-items: flex-start; }
  .oa-countdown { text-align: left; }
  .oa-countdown-figure { justify-content: flex-start; }
  .oa-grid { grid-template-columns: 1fr; }
  .block-container { padding-left: 1rem !important; padding-right: 1rem !important; }
}
</style>
"""


# --------------------------------------------------------------- icons

def icon(name: str, color: str = "currentColor", size: int = 15) -> str:
    """Inline SVG icon.

    Inline rather than an icon font: the mockup pulls Material Symbols from a
    Google CDN, which is one more network dependency that can fail on a
    deployment and leaves ligature text visible when it does.
    """
    paths = {
        "bank": "M4 10h3v7H4zM10.5 10h3v7h-3zM17 10h3v7h-3zM2 20h20v2H2zM12 2 2 7v2h20V7z",
        "cash": "M2 6h20v12H2zm10 2.5A3.5 3.5 0 1 0 15.5 12 3.5 3.5 0 0 0 12 8.5z",
        "trend": "M3 17l6-6 4 4 7-7v5h2V4h-8v2h5l-6 6-4-4-7 7z",
        "box": "M3 5h18v4H3zm1 6h16v9H4zm5 2v2h6v-2z",
        "shield": "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z",
        "gauge": "M12 4a9 9 0 0 0-9 9h3a6 6 0 1 1 12 0h3a9 9 0 0 0-9-9zm1 9a1 1 0 1 1-2 0l-.5-5h3z",
        "pulse": "M2 12h4l3-8 4 16 3-8h6",
        "block": "M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zM4 12a8 8 0 0 1 12.9-6.3L5.7 16.9A8 8 0 0 1 4 12zm8 8a8 8 0 0 1-4.9-1.7L18.3 7.1A8 8 0 0 1 12 20z",
        "check": "M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z",
    }
    d = paths.get(name, paths["pulse"])
    stroke = ' fill="none" stroke="currentColor" stroke-width="2"' if name == "pulse" else ' fill="currentColor"'
    return (
        f'<svg class="oa-card-icon" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'style="color:{color}" aria-hidden="true"><path d="{d}"{stroke}/></svg>'
    )


# -------------------------------------------------------------- builders

def metric_card(
    label: str,
    value: str,
    accent: str = "neutral",
    value_class: str = "t-on-surface",
    sub: str | None = None,
    icon_name: str | None = None,
    glow: bool = False,
) -> str:
    """One glass metric card: tracked label, optional icon, large figure."""
    glow_cls = " oa-glow-primary" if glow else ""
    icon_html = icon(icon_name) if icon_name else ""
    sub_html = f'<span class="oa-sub">{esc(sub)}</span>' if sub else ""
    return (
        f'<div class="oa-card oa-accent-{esc(accent)}">'
        f'<div class="oa-card-head"><span class="oa-label">{esc(label)}</span>{icon_html}</div>'
        f'<p class="oa-figure {esc(value_class)}{glow_cls}">{esc(value)}</p>'
        f"{sub_html}</div>"
    )


def card_grid(cards: list[str]) -> str:
    return f'<div class="oa-grid">{"".join(cards)}</div>'


def pnl_classes(value: float) -> tuple[str, str]:
    """Accent and text class for a P&L figure: green up, red down, dim flat."""
    if value > 0:
        return "secondary", "t-secondary"
    if value < 0:
        return "error", "t-error"
    return "neutral", "t-variant"


def status_pill(text: str, tone: str = "neutral") -> str:
    """A non-interactive status pill.

    Deliberately a ``<span>``, not a disabled button. This dashboard is public;
    there must be nothing on the page that looks like it could start or stop
    trading.
    """
    return f'<span class="oa-pill oa-pill-{esc(tone)}">{esc(text)}</span>'


def pill_row(pills: list[str]) -> str:
    return f'<div class="oa-pill-row">{"".join(pills)}</div>'


def market_banner(
    is_open: bool,
    countdown_units: list[tuple[str, str]],
    countdown_label: str,
    session_line: str,
) -> str:
    """The market status banner: pulsing orb, headline, countdown."""
    tone = "secondary" if is_open else "error"
    title = "MARKET OPEN" if is_open else "MARKET CLOSED"
    glyph = "check" if is_open else "block"
    units = "".join(
        f'<span>{esc(n)}<span class="u">{esc(u)}</span></span>' for n, u in countdown_units
    ) or '<span class="t-variant">—</span>'
    return (
        f'<div class="oa-banner{" is-open" if is_open else ""}">'
        f'<div class="oa-banner-left">'
        f'<div class="oa-orb">{icon(glyph, size=26)}</div>'
        f"<div>"
        f'<h2 class="oa-banner-title t-{tone} oa-glow-{tone}">{title}</h2>'
        f'<p class="oa-banner-sub">{esc(session_line)}</p>'
        f"</div></div>"
        f'<div class="oa-countdown">'
        f'<div class="oa-label" style="margin-bottom:5px">{esc(countdown_label)}</div>'
        f'<div class="oa-countdown-figure">{units}</div>'
        f"</div></div>"
    )


def regime_pill(regime: str) -> str:
    token = REGIME_COLORS.get(str(regime).upper(), "on_surface_variant")
    tone = {"tertiary": "tertiary", "secondary": "secondary", "primary": "primary"}.get(token, "neutral")
    return f'<span class="oa-pill oa-pill-{tone}">{esc(regime)}</span>'


def headroom_bar(label: str, used: float, limit_text: str, pct: float) -> str:
    """A limit-headroom bar. Warms to amber past 60% and red past 80%."""
    pct = max(0.0, min(1.0, pct))
    fill = "oa-bar-hot" if pct >= 0.8 else ("oa-bar-warn" if pct >= 0.6 else "oa-bar-ok")
    return (
        f'<div class="oa-bar-wrap">'
        f'<div class="oa-bar-head">'
        f'<span class="oa-label">{esc(label)}</span>'
        f'<span class="oa-bar-value">{esc(limit_text)}</span>'
        f"</div>"
        f'<div class="oa-bar-track"><div class="oa-bar-fill {fill}" style="width:{pct * 100:.1f}%"></div></div>'
        f"</div>"
    )


def table(headers: list[str], rows: list[list[str]], title: str, right_slot: str = "") -> str:
    """A styled table. Cells are pre-rendered HTML so callers can add pills."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="oa-table-wrap">'
        f'<div class="oa-table-head"><span class="oa-section-title">{esc(title)}</span>{right_slot}</div>'
        f'<div class="oa-table-scroll"><table class="oa-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></div>"
    )


def rule_row(number: str, name: str, threshold: str, state: str, observed: str = "") -> str:
    """One row of the Risk Gate panel."""
    cls = {"pass": "oa-rule-pass", "fail": "oa-rule-fail"}.get(state, "oa-rule-idle")
    glyph = {"pass": "✓", "fail": "✕"}.get(state, "·")
    meta = f"Limit {threshold}"
    if observed:
        meta += f" · observed {observed}"
    return (
        f'<div class="oa-rule">'
        f'<div class="oa-rule-badge {cls}">{esc(glyph)}</div>'
        f'<div class="oa-rule-body">'
        f'<div class="oa-rule-name">{esc(number)} · {esc(name)}</div>'
        f'<div class="oa-rule-meta">{esc(meta)}</div>'
        f"</div></div>"
    )


def rule_grid(rows: list[str]) -> str:
    return f'<div class="oa-rules">{"".join(rows)}</div>'


def rejection_card(
    rule_number: str,
    rule_name: str,
    title: str,
    reason: str,
    observed: str,
    limit: str,
) -> str:
    """A rejection, led by the rule that blocked it and the breaching value."""
    return (
        f'<div class="oa-reject">'
        f'<div class="oa-reject-head">'
        f'<span class="oa-reject-badge">{esc(rule_number)}</span>'
        f'<span class="oa-reject-title">{esc(rule_name)} — {esc(title)}</span>'
        f"</div>"
        f'<div class="oa-reject-reason">{esc(reason)}</div>'
        f'<div class="oa-breach">'
        f'<div><span class="oa-label">Limit</span>'
        f'<div class="oa-breach-num t-variant">{esc(limit)}</div></div>'
        f'<div><span class="oa-label">Actual</span>'
        f'<div class="oa-breach-num t-error">{esc(observed)}</div></div>'
        f"</div></div>"
    )


def terminal(lines: list[str], show_cursor: bool = True) -> str:
    """The journal terminal block. Lines are pre-escaped by ``terminal_line``."""
    body = "".join(lines) or '<p class="oa-term-line t-variant">&gt; No entries.</p>'
    cursor = '<span class="oa-cursor"></span>' if show_cursor else ""
    return f'<div class="oa-terminal"><div class="oa-term-inner">{body}{cursor}</div></div>'


def terminal_line(timestamp: str, event_type: str, text: str) -> str:
    """One colour-coded terminal line."""
    token = EVENT_COLORS.get(event_type, "on_surface_variant")
    cls = {
        "secondary": "t-secondary", "error": "t-error",
        "tertiary": "t-tertiary", "primary": "t-primary",
    }.get(token, "t-variant")
    return (
        f'<p class="oa-term-line {cls}">'
        f'<span class="t-variant">{esc(timestamp)}</span> '
        f"&gt; {esc(text)}</p>"
    )


def section_heading(title: str, anchor: str, right_slot: str = "") -> str:
    """A section title carrying an id, so nav anchors have somewhere to land."""
    return (
        f'<div id="{esc(anchor)}" style="display:flex;justify-content:space-between;'
        f'align-items:center;gap:12px;margin:6px 0 12px 0;scroll-margin-top:70px">'
        f'<h3 class="oa-section-title">{esc(title)}</h3>{right_slot}</div>'
    )
