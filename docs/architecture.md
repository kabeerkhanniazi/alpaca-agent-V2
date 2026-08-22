# Architecture

## One cycle

```
cron (*/5, market hours, CRON_TZ=America/New_York)
   │
   ▼
cron_runner.py ──► Orchestrator.run_cycle()
     │
     ├─1─ get_clock ──────────────► closed? journal a no-op and exit
     │
     ├─2─ build_portfolio()          account + positions + live Greeks
     │      └─ feeds the kill switch and Rules 3, 5, 9
     │
     ├─3─ manage_exits()             SPREAD-level, never per leg
     │      └─ orchestrator calls close_position (WRITE)
     │
     ├─4─ for each underlying:
     │      market_context()         spot, chain, bars, IV rank  ← runs for ALL
     │      run_model_loop()         ≤ 8 turns, read tools only
     │         └─ propose_spread ──► returns control to our code
     │
     ├─5─ validate_legs()            do both contracts exist and quote NOW?
     │
     ├─6─ risk_gate_check()          nine rules, pure, no network, no LLM
     │
     ├─7─ if approved AND may_execute:
     │      orchestrator calls place_option_order (WRITE)
     │      deterministic client_order_id → a crashed cycle cannot double-place
     │
     └─8─ journal every step → data/journal.jsonl
                                      │
                        scripts/push_journal.sh
                                      │
                            orphan `data` branch
                                      │
                          Streamlit Cloud dashboard
```

**Invariant, enforced by test:** the tool list passed to the model contains zero
write-capable tools. Its only route toward an order is `propose_spread`, which
returns control to deterministic code.

---

## The two doors

Filtering the schema list would be enough *if* a model could only act through
schemas it was given. It is not relied on alone:

1. **Schema door** — `MCPClient.tools_for_model()` builds the list by
   intersecting the server's tools with the read allowlist. A tool absent from
   the allowlist cannot appear even if the server offers it.
2. **Call door** — `MCPClient.call_read()` independently refuses any name outside
   the allowlist, so a fabricated or newly-added tool name is turned away at the
   call site rather than forwarded to Alpaca.

Write tools go through `call_write()`, restricted to the two the agent
legitimately needs (`place_option_order`, `close_position`), and reachable only
from the orchestrator.

---

## Where each concern lives

| Concern | Module | Ported? |
|---|---|---|
| Connect, filter, call | `agent/mcp_client.py` | new |
| `propose_spread` schema + validation | `agent/tools.py` | new |
| Provider-agnostic chat-with-tools | `agent/llm.py` | new |
| The analyst prompt, versioned | `agent/prompts.py` | new |
| The cycle | `agent/orchestrator.py` | new |
| MCP JSON → ported shapes | `agent/adapters.py` | replaces `broker.py` |
| Nine risk rules | `agent/risk_gate.py` | ported |
| Credit / max-loss / POP | `agent/spread_builder.py` | ported |
| Chain → candidates | `agent/options_calculator.py` | ported |
| Spread-level exits | `agent/position_manager.py` | ported |
| IV rank + rv proxy | `agent/iv.py` | ported |
| JSONL journal | `agent/journal.py` | ported + agent events |
| Dashboard | `streamlit_app.py`, `agent/dashboard_theme.py` | ported + Agent Reasoning |

The ported modules take dicts in and return dicts out. Only their **data source**
changed — from `alpaca-py` return objects to MCP tool-result JSON — which is what
`adapters.py` exists to absorb.

---

## Why the adapter is an attribute view

The ported code reads snapshots with `getattr(snapshot, "implied_volatility",
None)`, `.latest_quote.bid_price`, `.greeks.delta`. MCP returns plain JSON in
Alpaca's wire format: `impliedVolatility` (a *sibling* of `greeks`, not inside
it), `latestQuote` with one-letter `bp`/`ap` keys. Resolving those on attribute
lookup keeps the mapping in one place and leaves the ported logic untouched.

Three coercions are load-bearing rather than cosmetic:

- Account money fields arrive as **strings**. Rules 2, 3 and 9 do arithmetic on
  them; a string reaching Rule 2 either raises or silently string-multiplies.
- Timestamps carry **more precision than `fromisoformat` accepts** and are
  truncated first.
- Contracts on the indicative feed routinely arrive with **no `greeks` key**.
  That is normal for illiquid strikes and must be skipped, not raised on.

---

## Journal event types

Additive to the previous build's schema, so ported analytics keep working.

| Event | Meaning |
|---|---|
| `analysis` | Deterministic market context per underlying — runs even when no trade follows |
| `agent_turn` | One model turn: tools called, arguments, result sizes |
| `agent_proposal` | The `propose_spread` payload including the rationale verbatim |
| `no_proposal_declined` | **Healthy** — the model judged conditions poor |
| `no_proposal_turn_limit` | **Defect** — ran out of turns |
| `malformed_proposal` | **Defect** — schema violation |
| `invalid_proposal` | **Defect** — legs do not exist or cannot be quoted |
| `provider_switch` | A failover, so a tonal shift in rationales is explainable |
| `trade_approved` / `trade_rejected` | Every rule with `passed`, `observed`, `limit` |
| `order_dry_run` | A simulated trade. Deliberately **not** `order_submitted` |
| `order_submitted` / `order_filled` / `order_failed` | Real orders only |
| `position_exit` | A spread-level close |
| `cycle_summary` | Provider, model, turns used, mode, counts |

`order_dry_run` being a separate type is not cosmetic: journalling dry runs as
`order_submitted` would show judges "8 orders, $1,968 collected" with nothing
traded.
