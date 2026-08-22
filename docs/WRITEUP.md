# MCP-Mediated Options Agent — Write-up

An autonomous options agent where a language model reasons over live Alpaca data
through Alpaca's MCP server and proposes vertical credit spreads, and a
deterministic risk gate — which the model cannot see, call, or influence —
decides whether any proposal reaches an execution tool.

---

## AI logic

Every five minutes during market hours, the agent runs one cycle per underlying
(SPY, QQQ, IWM). For each, a language model is given a research task and a
tool-calling loop capped at **8 turns**: it checks the market clock, the account,
existing positions, then the option chain, and either calls `propose_spread` once
or declines in plain text.

`propose_spread` is a **synthetic tool** — not an Alpaca tool. Calling it places
nothing. It ends the model's turn and returns control to deterministic code,
carrying the strikes, expiry, size, and a required free-text rationale.

The model is reached through a provider-agnostic layer with one interface,
`chat(messages, tools)`. The primary is **`nvidia/nemotron-3-ultra-550b-a55b:free`
via OpenRouter**, with Groq (`openai/gpt-oss-120b`) and Gemini 3.6 Flash as
fallbacks. The primary was chosen empirically, not by reputation: a two-tool
round-trip harness was run against all 17 free tool-calling models on OpenRouter
and 12 passed. Failover on a 429 or 5xx is automatic and **journalled as an
event**, so any tonal shift in the rationales is explainable rather than
mysterious. Provider and model are recorded on every cycle.

Every rationale is journalled verbatim and rendered on the dashboard. A rationale
citing no numbers is flagged as thin — surfaced rather than suppressed, because a
model producing confident text with no evidence is exactly the thing worth seeing.

---

## Risk gates

Nine deterministic rules, no model, no network, thresholds in
`config/risk_config.json` and never as literals in the checking code. Every rule
returns `{rule, passed, observed, limit, detail}`, so a rejection is always
explainable without re-deriving anything.

| # | Rule | Threshold |
|---|---|---|
| 1 | Short-leg delta | `abs(delta) ≤ 0.20` |
| 2 | Notional | `max_loss × contracts ≤ 2% of NAV` |
| 3 | Portfolio delta-dollars | `≤ 50% of NAV` (sizes down rather than rejecting) |
| 4 | Minimum credit | `≥ $25` per contract |
| 5 | Duplicate strike | none open at the same underlying/expiry/strike |
| 6–7 | DTE window | `7 ≤ dte ≤ 14` |
| 8 | Kill switch | daily loss `> 5%` rejects everything |
| 9 | Buying power | `> 20%` of starting reserve remains |

Rule 3 is documented at 0.50 with the originally-planned 0.10 retained beside it
in the config: a single 15-delta spread sized to the 2% budget carries roughly
$32k of delta-dollars on $100k, so 0.10 would reject every trade the strategy is
designed to produce.

**Why the model cannot reach the write tools.** The MCP server exposes 74 tools,
17 of which write. The model is handed 11 read tools plus `propose_spread` —
**zero write tools**, not disabled or discouraged but absent from the list it is
given. There is no prompt injection or hallucination that reaches an order,
because the capability was never handed over.

`ALPACA_TOOLSETS` scoping is applied as defence in depth, but it **cannot**
express this split: the `trading` toolset bundles `get_all_positions`, which the
agent genuinely needs, together with `place_option_order`, `close_all_positions`
and seven other write tools. No value of that variable yields position reads
without order placement. The in-code allowlist is therefore the enforcement
point, and it is built by **inclusion** — a subtractive allowlist would have
missed `create_locate`, a write tool belonging to no documented toolset at all.

Two further guards: the call site independently refuses any tool outside the
allowlist, so even a fabricated name is turned away rather than forwarded; and a
**leg-validation step** runs between the proposal and the gate, re-quoting both
contracts to confirm they exist and are tradeable. Without it, a strike read from
a chain the model saw several turns ago surfaces as a confusing Rule 4 credit
failure instead of what it is.

Four no-order outcomes are kept distinct because two are healthy and two are
defects: `no_proposal_declined`, `no_proposal_turn_limit`, `malformed_proposal`,
`invalid_proposal`.

---

## Alpaca infrastructure

All Alpaca I/O goes through **Alpaca's official MCP server** (`alpaca-mcp-server`
v3.4.7, FastMCP), launched as a stdio subprocess with `uvx`. The `alpaca-py` SDK
is not imported anywhere in the agent path, and a test fails the build if it ever
is.

Read tools used: `get_clock`, `get_account_info`, `get_all_positions`,
`get_open_position`, `get_orders`, `get_option_chain`, `get_option_snapshot`,
`get_option_contracts`, `get_stock_bars`, `get_stock_latest_trade`,
`get_stock_snapshot`. The orchestrator — never the model — additionally calls
`place_option_order` on approval and `close_position` for exits.

Paper trading throughout. **Data feeds are free-tier: `iex` for stock bars and
`indicative` for options.** SIP and OPRA require paid plans, and OPRA
additionally a signed agreement — the feed is stated in the journal alongside
each quote rather than glossed over. Indicative quotes carry full Greeks and
implied volatility and are adequate for strike selection.

Three behaviours were established against the live server rather than assumed:
a multi-leg **credit submits as a negative `limit_price`** with every scalar as a
string (confirmed by an accepted-then-cancelled order); options calls must pass
`feed="indicative"` explicitly because the schema default `opra` returns 403; and
`get_option_chain` truncates a **strike-ascending** ordering, so requests are
bracketed around spot — unbracketed, a SPY put chain returns only worthless
deep-OTM strikes carrying no Greeks.

Worth noting: the server labels its own tool output `untrusted_tool_output` with
an instruction to treat it as data rather than instructions. That is Alpaca doing
prompt-injection hygiene at the transport layer, and it complements the
containment argument above.

Exits are evaluated at **spread level**, never per leg. Alpaca reports the two
legs of a vertical as separate positions, so a short leg decaying into profit
while its long wing decays into loss would, judged per leg, close the short alone
and leave a naked long put behind.
