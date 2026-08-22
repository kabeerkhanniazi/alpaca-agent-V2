# MCP-Mediated Options Agent

An autonomous options trading agent for Alpaca paper accounts. A language model
reasons over live market data **through Alpaca's MCP server** and proposes
vertical credit spreads. A deterministic nine-rule risk gate decides whether any
proposal becomes an order.

**The model is never given a write-capable tool.** Not disabled, not discouraged
by prompt — absent from the tool list it receives.

---

## The one design decision everything follows from

```
                    ┌──────────────────────────────────────┐
                    │  LLM ANALYST (sees only read tools)  │
                    │  + one synthetic tool: propose_spread│
                    └──────────────┬───────────────────────┘
                                   │ multi-turn tool calling
                     ┌─────────────▼──────────────┐
                     │  Alpaca MCP server         │
                     │  11 READ tools allowlisted │
                     │  (74 exposed, 17 of them   │
                     │   write — none reachable)  │
                     └─────────────┬──────────────┘
                                   │
                  model calls propose_spread(...)
                                   │
                     ┌─────────────▼──────────────┐
                     │  LEG VALIDATION            │
                     │  do both contracts exist   │
                     │  and quote right now?      │
                     └─────────────┬──────────────┘
                     ┌─────────────▼──────────────┐
                     │  RISK GATE (9 rules)       │
                     │  pure · deterministic      │
                     │  no LLM · no network       │
                     └──────┬──────────────┬──────┘
                       APPROVE          REJECT
                            │              │
              ┌─────────────▼───┐          ▼
              │ ORCHESTRATOR    │      journal only
              │ calls MCP WRITE │
              │ tool            │
              └─────────────────┘
```

`propose_spread` is not an Alpaca tool. Calling it places nothing — it ends the
model's turn and hands control back to deterministic code. There is no prompt
injection or hallucination that reaches an order, because the capability was
never handed over.

Measured on the live server:

| | |
|---|---|
| Tools the server exposes by default | 74 |
| After `ALPACA_TOOLSETS` scoping | 53 |
| Write tools present in the process | 11 |
| Tools handed to the model | **12** (11 read + `propose_spread`) |
| Write tools reachable by the model | **0** |

---

## Strategy

Bull put credit spreads on SPY, QQQ and IWM. Nothing else is traded — no naked
short, no long premium, no equity.

- Short leg delta **−0.20 to −0.15**
- **7–14** days to expiry
- Spread width **$5–$10** (a dollar distance; SPY strikes are $1 apart)
- Minimum credit **$25** per contract
- Exits at 50% of credit captured, a stop at −100% of credit, forced close at 2 DTE

Exits are evaluated at **spread level**. Alpaca reports the two legs as separate
positions, and closing a profitable short leg on its own would orphan the long put.

---

## Quick start

```bash
git clone <repo> && cd alpaca-mcp-agent
curl -LsSf https://astral.sh/uv/install.sh | sh   # the MCP server runs via uvx
uv venv && uv pip install -r requirements.txt
cp .env.example .env                              # then fill it in
```

`.env` needs Alpaca paper keys (options level 3) and at least one LLM key:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER_TRADE=true
OPENROUTER_API_KEY=...      # primary
GROQ_API_KEY=...            # fallback
GEMINI_API_KEY=...          # second fallback
```

Run one cycle without placing anything:

```bash
.venv/bin/python cron_runner.py --dry-run --force --ticker SPY
```

`--dry-run` is the default and `--live` must be given explicitly. `--force`
bypasses the market-hours gate for testing outside trading hours.

Dashboard:

```bash
.venv/bin/streamlit run streamlit_app.py
```

Schedule it (installs in dry-run mode; going live is a separate deliberate act):

```bash
./install_cron.sh
```

---

## Layout

```
agent/
├── mcp_client.py        connect, filter to the allowlist, call  ← the mediation
├── tools.py             propose_spread schema + validation
├── llm.py               provider-agnostic chat-with-tools
├── prompts.py           the analyst prompt, versioned
├── orchestrator.py      the cycle
├── adapters.py          MCP JSON → the ported modules' shapes
├── risk_gate.py         nine rules          ┐
├── spread_builder.py    credit/max-loss/POP │ ported from the
├── options_calculator.py chain → candidates │ previous build,
├── position_manager.py  spread-level exits  │ logic untouched
├── iv.py                IV rank + rv proxy  │
└── journal.py           JSONL journal       ┘
config/
├── risk_config.json     the nine thresholds
├── options_config.json  strategy knobs
├── agent_config.json    model loop + MCP layer
└── mcp_allowlist.json   the tools the model may see
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q                      # 433 tests
.venv/bin/python -m pytest tests/ -q -m "not integration" # fully offline
```

Two of these encode compliance and safety. If either is deleted or weakened, the
build has regressed regardless of what else passes:

- **`test_tool_mediation.py`** — no write-capable tool appears in the list given
  to the model. Each of the 17 write tools is asserted absent individually, and
  each is refused at the call site as well.
- **`test_no_sdk_import.py`** — `alpaca-py` is not imported anywhere under
  `agent/`. All Alpaca access goes through the MCP server.

---

## Data feeds

Free tier: `iex` for stock bars, `indicative` for options. SIP and OPRA need paid
plans, and OPRA a signed agreement. Indicative quotes carry full Greeks and IV
and are adequate for strike selection. The feed is recorded in the journal
alongside each quote.

`docs/mcp_tools.md` records the complete tool inventory, the captured JSON shapes
the adapter is written against, and the traps found along the way.
