# STACK — MCP-Mediated Options Agent

Technical reference. `PLAN.md` says what to build and in what order; this says what with.

---

## 1. Architecture

```
cron (*/5, market hours)
   │
   ▼
cron_runner.py ──► orchestrator.run_cycle(ticker)
                        │
                        ├─1─ portfolio state + daily P&L  ─┐
                        ├─2─ market clock                  ├─ via MCP read tools
                        ├─3─ spread-level exit management ─┘
                        │
                        ├─4─ LLM loop ────────────────────────────────┐
                        │      model ⇄ MCP read tools (allowlisted)   │
                        │      model → propose_spread(...)  ──────────┘
                        │
                        ├─5─ risk_gate.check(proposal, portfolio, account, daily_loss)
                        │      pure · deterministic · no network · no LLM
                        │
                        ├─6─ if approved: ORCHESTRATOR calls MCP write tool
                        │
                        └─7─ journal every step → data/journal.jsonl
                                                        │
                                          scripts/push_journal.sh
                                                        │
                                              orphan `data` branch
                                                        │
                                            Streamlit Cloud dashboard
```

**Invariant, enforced by test:** the tool list passed to the model contains zero
write-capable tools. The model's only route to an order is `propose_spread`, and that
returns control to deterministic code.

---

## 2. Dependencies

| Package | Purpose | Notes |
|---|---|---|
| `mcp` | MCP client — connect to the Alpaca server, list and call tools | Verify current package name and client API against Alpaca's docs |
| `httpx` | LLM provider HTTP | Async-capable; the MCP client is async, so keep the whole loop async |
| `pandas` | Greeks aggregation, position tables | Ported code depends on it |
| `python-dotenv` | Config | |
| `streamlit` | Dashboard | |
| `plotly` | P&L chart — themeable from design tokens, unlike Vega | Must be in `requirements.txt`, not just pip-installed |
| `pytest`, `pytest-mock`, `pytest-asyncio` | Tests | `asyncio` needed for the MCP client |

**Not a dependency: `alpaca-py`.** Its presence in the agent path is a compliance failure.
Add a test that fails if `alpaca` is imported anywhere under `agent/`.

---

## 3. LLM provider

Available: **Gemini, Groq, OpenRouter**. No direct Anthropic or OpenAI key.

Requirement: native multi-turn tool calling.

| Provider | Role | Why |
|---|---|---|
| **OpenRouter → Anthropic model** | Primary | OpenRouter serves Anthropic models, so "Claude talking to Alpaca via MCP" — the hackathon's own framing of the theme — is reachable without an Anthropic key |
| **Groq** | Fallback | Fast, generous limits, solid tool calling. Takes over on rate limit or 5xx |
| **Gemini** | Second fallback | Optional |

`agent/llm.py` exposes one interface:

```
chat(messages, tools) -> {text, tool_calls, provider, model, usage}
```

Each provider is an adapter behind it. Provider and model come from config, never from
literals in the loop. **Journal the provider and model on every cycle** so the write-up and
the video can state accurately which model was driving.

Fallback logic: on 429 or 5xx, retry once with backoff on the primary, then switch. Journal
the switch as an event — it is honest and it explains any tonal shift in the rationales.

---

## 4. MCP server

**Verify all of the following against Alpaca's current docs before coding.** The server went
through a v1 → v2 rewrite in which tool names changed. Anything stated from memory is a
liability here.

Establish and record in `docs/mcp_tools.md`:

- Package name and invocation (`uvx`-style launch vs pip install vs binary)
- Transport (stdio subprocess vs HTTP) and how credentials are passed
- The **complete tool list**, each marked `READ` or `WRITE`
- Whether toolsets can be scoped at launch (e.g. an `ALPACA_TOOLSETS` env var). If they can,
  use it as defence in depth — but the allowlist in code is still the enforcement point,
  because it is the thing under test
- The **raw JSON shape** of each tool result you depend on. `agent/adapters.py` is written
  against these captured shapes, not against assumptions

Read tools the agent needs: market clock, account, positions, option chain / snapshots with
Greeks, stock bars (for IV rank's realized-vol proxy).

Write tool the **orchestrator** needs: multi-leg order placement, and position closing for
exits.

### Multi-leg order convention

In `alpaca-py`, an `MLEG` order takes a positive `limit_price` for a debit and a **negative**
one for a credit — so credit spreads submit negative. The MCP tool schema may use the same
convention, the opposite one, or an explicit side field. **Determine which, and unit-test it.**
Getting this backwards submits an inverted order, and it is the single most likely serious
bug in the execution path.

---

## 5. File layout

```
alpaca-mcp-agent/
├── .env                          # never committed
├── .env.example
├── .gitignore                    # .env, venv/, logs/, data/, __pycache__/, .pytest_cache/
├── requirements.txt
├── pyproject.toml
├── README.md
├── SUBMISSION.md                 # account ID, URLs, post links
│
├── config/
│   ├── risk_config.json          # nine thresholds — §7
│   ├── agent_config.json         # underlyings, cadence, turn cap, provider, exits
│   └── mcp_allowlist.json        # read tool names given to the model
│
├── agent/
│   ├── mcp_client.py             # connect, list, filter to allowlist, call
│   ├── llm.py                    # provider-agnostic chat-with-tools
│   ├── tools.py                  # propose_spread schema + handler
│   ├── prompts.py                # analyst system prompt, versioned
│   ├── orchestrator.py           # the cycle
│   ├── adapters.py               # MCP JSON → ported-module input shapes
│   ├── risk_gate.py              # PORTED — do not rewrite
│   ├── spread_builder.py         # PORTED
│   ├── iv.py                     # PORTED
│   ├── position_manager.py       # PORTED — spread-level exits
│   └── journal.py                # PORTED + new agent event types
│
├── cron_runner.py
├── install_cron.sh
├── streamlit_app.py              # PORTED + Agent Reasoning panel
├── dashboard_theme.py            # PORTED
│
├── scripts/push_journal.sh       # PORTED — orphan-branch publishing
│
├── data/                         # journal.jsonl, iv_history.jsonl (gitignored)
├── logs/
│
├── tests/
│   ├── test_tool_mediation.py    # ★ no write tool reaches the model
│   ├── test_no_sdk_import.py     # ★ alpaca-py absent from agent path
│   ├── test_risk_gate.py         # PORTED — 9 rules × pass/fail + boundaries
│   ├── test_spread_builder.py    # PORTED
│   ├── test_iv.py                # PORTED
│   ├── test_adapters.py          # MCP JSON fixtures → expected shapes
│   ├── test_orchestrator.py      # mocked MCP + mocked LLM, full cycle
│   ├── test_order_sign.py        # credit spread submits as a credit
│   └── test_dashboard.py         # PORTED
│
└── docs/
    ├── mcp_tools.md              # tool inventory + captured JSON shapes
    ├── WRITEUP.md                # the required one-pager
    └── architecture.md
```

The two `★` tests are the ones that encode compliance and safety. If either is deleted or
weakened, the build has regressed regardless of what else passes.

---

## 6. The analyst prompt

Lives in `agent/prompts.py`, versioned, and journalled by version so a change in behaviour
is traceable to a change in prompt.

It must establish:

- **Role.** An options analyst selling premium on liquid ETFs. Not a directional trader.
- **Instrument constraint.** Vertical credit spreads only. Never naked short. Never long
  premium. Never equity.
- **The tools available** and that research is expected before proposing — check the clock,
  the account, existing positions, then the chain.
- **Target zone.** Short leg delta −0.20 to −0.15, DTE 7–14, spread width 5–10.
- **That a risk gate will independently evaluate the proposal**, and that a rejection is a
  normal outcome, not a failure. This matters: without it, models get sycophantic and start
  proposing whatever they think will be approved rather than what the data supports.
- **The rationale requirement.** Cite the specific numbers observed — IV rank, the delta of
  the chosen strike, the credit, existing portfolio exposure. A rationale with no numbers in
  it is a bad rationale and should be visible as such on the dashboard.
- **Permission to decline.** If conditions are poor — IV too low to be paid for the risk, no
  strike in the delta window, portfolio already concentrated — say so and propose nothing.
  A model that always proposes is not analysing.

Keep it under ~400 words. Long prompts degrade tool-calling reliability more than they
improve reasoning.

---

## 7. Risk gate — thresholds

From `config/risk_config.json`. Never literals in the function body. Every rule returns
`{rule, name, passed, observed, limit, detail}` so a rejection is always explainable and the
dashboard can render it without re-deriving anything.

| # | Rule | Threshold | Rationale |
|---|---|---|---|
| 1 | Short-leg delta | `abs(delta) ≤ 0.20` | ~20% ITM probability; keeps distance from ATM |
| 2 | Notional | `max_loss × contracts ≤ 2% of NAV` | $2,000 on $100k. `contracts = floor(2000 / max_loss_per_spread)` |
| 3 | Portfolio delta | `abs(Σ delta × 100 × contracts × spot) ≤ 0.50 × NAV` | **Delta-dollars.** 0.10 rejects every trade the strategy produces — see `PLAN.md` §3.4. Gate **sizes down** to fit headroom rather than rejecting outright |
| 4 | Minimum credit | `net_credit ≥ $25` per contract | Below this, execution slippage eats the edge |
| 5 | Duplicate strike | No open position at same `(underlying, expiry, strike)` | Concentration control |
| 6 | Minimum DTE | `≥ 7` | Avoids terminal gamma |
| 7 | Maximum DTE | `≤ 14` | Avoids slow theta |
| 8 | Kill-switch | `daily_loss_pct > 5%` → reject everything | Circuit breaker, no exceptions |
| 9 | Buying power | remaining after trade `> 20%` of starting | Cushion against deep-ITM moves |

Keep `"plan_original": 0.10` alongside Rule 3's value with a comment, as the previous build
did. Documenting a deliberate deviation is worth more than silently shipping the number that
works.

---

## 8. Agent config

`config/agent_config.json`:

- `underlyings`: SPY, QQQ, IWM
- `cycle_interval_seconds`: 300
- `max_turns_per_ticker`: 5–8 — hard cap on the model's tool-calling loop
- `cycle_timeout_seconds`: wall-clock ceiling per ticker; a hung provider must not stall cron
- `max_new_positions_per_cycle`: 1 — but **analyse all three underlyings regardless**, because
  the dashboard's market panel needs IV rank for each. Cap execution, not analysis. (This was
  a real bug in the previous build: capping analysis silently skipped QQQ and IWM entirely.)
- `provider` / `model` / `fallback_provider`
- `exits`: 50% profit target, stop at −100% of credit, force-close at DTE ≤ 2
- `delta_range`: [−0.20, −0.15]; `dte_range`: [7, 14]; `spread_width`: [5, 10]

---

## 9. Data feed constraints

Free tier gives the `iex` stock feed and the `indicative` options feed. SIP bars and the OPRA
options feed need paid plans, and OPRA additionally requires a signed agreement. Indicative
quotes are adequate for strike selection. State the feed in the write-up — judges from Alpaca
will know exactly what the free tier provides, and claiming otherwise is worse than the
limitation itself.

Note in the journal which feed produced each quote, the same way IV rank already labels
`iv_history` vs `rv_proxy`.

---

## 10. Dashboard

Port the existing restyled dashboard whole — theme module, WCAG-checked tokens, degraded
mode, orphan-branch journal source, Plotly P&L chart, all four journal tabs.

**Add one panel: Agent Reasoning.** For the most recent cycle per underlying —

- which tools the model called, in order, with argument summaries
- its rationale text, verbatim
- the gate verdict beside it, with the failing rule highlighted on rejection
- provider, model, and turns used

This panel *is* the "AI agent" evidence. Everything else on the dashboard could have been
produced by a script.

Keep the read-only guarantee: `MODE: DRY-RUN | LIVE` and `KILL-SWITCH: ARMED | TRIPPED` are
non-interactive `<span>` elements, not buttons. The dashboard is public; nothing on it should
be able to start, stop, or alter trading.

---

## 11. Verification

Before go-live, all of these:

1. `pytest tests/ -q` green, including both `★` compliance tests
2. `python cron_runner.py --dry-run --force --ticker SPY` — real MCP calls, real model turns,
   real gate verdict, no order; journal contains the model's rationale
3. Force a rejection deliberately (tighten a threshold in config) and confirm the rejection
   card renders with rule number, limit, and observed value
4. Dashboard renders with live data **and** with credentials stubbed out — zero exceptions in
   both
5. `env -i HOME=$HOME PATH=/usr/bin:/bin bash -c 'cd <repo> && ./scripts/push_journal.sh'`
   exits 0
6. Layout checked at 1440px and on a real phone
7. Grep the whole `agent/` tree for `alpaca_py` / `import alpaca` — zero hits
