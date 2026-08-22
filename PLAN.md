# PLAN — MCP-Mediated Options Agent

Build target: an autonomous options trading agent where an LLM analyst reasons over live
Alpaca data **through the Alpaca MCP server**, proposes defined-risk credit spreads, and a
deterministic risk gate — which the LLM cannot see, call, or influence — decides whether
those proposals ever reach an execution tool.

This is a single-pass build. It is not organised by day. Work the dependency chain top to
bottom; each stage has a verification gate that must pass before the next begins.

---

## 0. Compliance map — read this first

Every core requirement from the hackathon page, and exactly where it is satisfied. If a
change breaks one of these, the change is wrong.

| Requirement (verbatim) | Where satisfied | Failure mode if dropped |
|---|---|---|
| "Autonomous AI trading agents using Alpaca's Trading API" | `agent/orchestrator.py` — an LLM drives a multi-turn tool-calling loop each cycle, unattended via cron | A deterministic pipeline with no model in it is not an "AI trading agent" |
| "projects must utilize either Alpaca's MCP server or its CLI tools" | **All** Alpaca I/O goes through the MCP server. The `alpaca-py` SDK is not imported anywhere in the agent path | This is the requirement the previous build missed. Do not reintroduce direct SDK calls |
| "all strategies must incorporate options trading" | Only instrument traded is a vertical credit spread on SPY / QQQ / IWM. No equity legs, ever | — |
| "Competition account starting balance must be set to $100,000" | Fresh paper account created for judging; config asserts NAV ≈ 100000 at first live cycle and logs a warning otherwise | Ineligible |
| "brand-new Alpaca paper trading account" for judging | Created immediately before go-live; account ID recorded in `SUBMISSION.md` | Ineligible |
| "One-page write-up covering your AI logic, risk gates, and Alpaca infrastructure" | `docs/WRITEUP.md`, one page, three sections matching those three words | Missing deliverable |

Two secondary asks, both cheap, both scored: up to **5 social posts** tagging `@lablabai` /
`@AlpacaHQ` on X and lablab.ai / Alpaca on LinkedIn, and a public repo + hosted dashboard.

---

## 1. The one design decision everything else follows from

The Alpaca MCP server exposes a large toolset — read tools and write tools together. The
naive integration hands all of them to the model and lets it trade. Do not do that. It is
unsafe, and it is also the least interesting thing to demo.

**Split the toolset by trust level:**

```
                    ┌─────────────────────────────────────┐
                    │  LLM ANALYST  (sees only read tools)│
                    │  + one synthetic tool: propose_spread│
                    └──────────────┬──────────────────────┘
                                   │ multi-turn tool calling
                     ┌─────────────▼──────────────┐
                     │  Alpaca MCP server         │
                     │  READ-ONLY allowlist:      │
                     │  clock, account, positions,│
                     │  option chain, snapshots,  │
                     │  bars                      │
                     └─────────────┬──────────────┘
                                   │
                     LLM calls propose_spread(...) ──► control returns to our code
                                   │
                     ┌─────────────▼──────────────┐
                     │  RISK GATE (9 rules)       │
                     │  pure, deterministic,      │
                     │  no LLM, no network        │
                     └──────┬──────────────┬──────┘
                       APPROVE          REJECT
                            │              │
              ┌─────────────▼───┐          ▼
              │ ORCHESTRATOR    │      journal only
              │ calls MCP WRITE │
              │ tool (place     │
              │ order)          │
              └─────────────────┘
```

**The model never receives a write tool.** Not disabled, not discouraged by prompt —
absent from the tool list it is given. There is no prompt injection, jailbreak, or
hallucination that lets it place an order, because the capability was never handed over.

This is the demo. The strongest thirty seconds of the video is a journal entry showing the
model's reasoning for a trade, followed by `Rule 2: max_loss 2,140 > limit 2,000 → REJECTED`,
followed by no order. Judges have seen LLMs place trades. They have not seen one credibly
prevented from doing so.

Write a test that fails if any write-capable tool name appears in the list passed to the
model. That test is the most important one in the suite.

---

## 2. What to port and what to build

Roughly 60% of this exists and is proven. **Port it — do not rewrite it.** Source repo:
the previous build at `~/alpaca-agent` (branch `main`, 271 passing tests).

### Port as-is (adapt the data source only)

| Module | Why it's worth porting |
|---|---|
| `risk_gate.py` | Nine rules, 50 tests, and several corrected constants that cost real debugging (see §3) |
| `spread_builder.py` | Credit / max-loss / POP arithmetic verified against hand-computed fixtures |
| `iv.py` | IV rank with the `iv_history` → `rv_proxy` cold-start fallback, and honest source labelling |
| `trade_journal.py` | JSONL journal incl. the torn-line self-heal |
| `dashboard_theme.py` + `streamlit_app.py` | Full restyle, measured WCAG contrast, degraded-mode rendering |
| `scripts/push_journal.sh` | Orphan-branch journal publishing so the deployed dashboard has data |
| `tests/` | Everything except the tests that assert on live Alpaca content |

The port is mechanical: these modules take dicts in and return dicts out. Only their
**data source** changes — from `alpaca-py` return objects to MCP tool-result JSON. Write one
adapter module that normalises MCP JSON into the shapes the ported modules already expect.
Do not touch the ported logic.

### Build new

- `agent/mcp_client.py` — spawn/connect the MCP server, list tools, enforce the allowlist
- `agent/orchestrator.py` — the LLM tool-calling loop
- `agent/llm.py` — provider-agnostic chat-with-tools wrapper
- `agent/tools.py` — the synthetic `propose_spread` tool schema and handler
- `agent/adapters.py` — MCP JSON → the shapes the ported modules expect
- `agent/prompts.py` — the analyst system prompt, versioned
- New journal event types for agent reasoning (see §5)

---

## 3. Bugs already paid for — port the fixes, do not rediscover them

Each of these was found the expensive way. Every one has a test in the source repo; port
the test alongside the fix.

1. **Net delta sign.** Computed as `short − long`, which labels every bull put spread
   bearish and feeds Rule 3 a sign-flipped number. Get the sign right and pin it.
2. **Exits must be spread-level, not leg-level.** Alpaca reports the two legs as separate
   positions. A short leg hitting +50% while its long wing sits at −$160 will close the
   short alone and **orphan the long put**. Group legs into spreads before evaluating exits.
3. **Dry runs must not journal as `order_submitted`.** A week of dry runs otherwise shows
   judges "8 orders, $1,968 collected" with nothing traded. Separate event types.
4. **Rule 3 at 0.10 rejects every trade the strategy produces.** One 15-delta spread at 2%
   size carries ~$32k of delta-dollars on $100k against a $10k cap. Use **0.50** as
   delta-dollars, document the reasoning in the config next to the original value, and have
   the gate **size down to fit headroom** rather than reject outright.
5. **Multi-leg limit price sign.** For `OrderClass.MLEG`, a positive `limit_price` is a
   **debit** and a negative one is a **credit**. A credit spread submits negative. Verify
   the equivalent convention in the MCP tool schema — it may differ — and unit-test it.
6. **`outline` is a border-only colour token.** At 2.46:1 it fails WCAG AA as text. Test
   that `color: var(--outline)` never appears.
7. **Journal writes must self-heal a torn final line** — otherwise one interrupted write
   corrupts the *next* one too.
8. **No test may assert on live Alpaca content.** Alpaca had an outage mid-build last time
   and reddened the suite. Tests accept either the populated panel or the documented
   degraded placeholder.

---

## 4. Build order

Each stage ends with a gate. Do not proceed past a failing gate.

### Stage 1 — MCP server, standalone

Install and run the Alpaca MCP server against the existing dev paper account. **Verify the
current package name, invocation, tool names, and toolset-scoping mechanism from Alpaca's
own docs — do not trust any of it from memory, the server has been through a rewrite that
renamed tools.**

Enumerate every tool it exposes and write the full list to `docs/mcp_tools.md`, each marked
`READ` or `WRITE`. This file is the input to the allowlist and to the safety test.

> **Gate:** a throwaway script connects to the server, lists tools, and successfully fetches
> the market clock, the account, and one SPY option chain. Paste the raw JSON shape of each
> into `docs/mcp_tools.md` — the adapter is written against these shapes, not against guesses.

### Stage 2 — LLM provider

Available keys: **Gemini, Groq, OpenRouter** (no direct Anthropic or OpenAI key).

Requirement: native tool/function calling with multi-turn. All three qualify. **OpenRouter
can serve Anthropic models**, so a Claude-driven agent is reachable without an Anthropic key
— which matters, because "Claude talking to Alpaca via MCP" is the hackathon's own framing
of the theme.

Build `agent/llm.py` provider-agnostic behind one interface — `chat(messages, tools) →
{text, tool_calls}` — with the provider set in config. Default to Claude via OpenRouter;
fall back to Groq on rate limit or 5xx. Log which provider and model served each cycle into
the journal, so the submission can state it accurately.

> **Gate:** a script sends a two-tool toy schema to the configured provider and correctly
> round-trips a tool call and its result, on both the primary and the fallback provider.

### Stage 3 — The mediation layer

`agent/mcp_client.py` and `agent/tools.py`. Filter the MCP tool list to the READ allowlist,
append the synthetic `propose_spread` schema, and hand only that combined list to the model.

`propose_spread` schema takes: `underlying`, `short_strike`, `long_strike`, `expiry`,
`contracts_requested`, and a required free-text `rationale`. The rationale is not decorative
— it is what gets journalled and shown on the dashboard, and it is the evidence that a model
actually reasoned rather than pattern-matched.

> **Gate:** the safety test passes — no write-capable tool name appears in the list given to
> the model — and it fails loudly if someone later adds one.

### Stage 4 — Port the deterministic core

Bring over the risk gate, spread builder, IV module, and journal. Write `agent/adapters.py`
to normalise MCP JSON into their existing input shapes.

> **Gate:** the ported test suites pass unmodified against adapter-produced fixtures. If a
> ported test needs its logic changed to pass, the adapter is wrong, not the test.

### Stage 5 — The orchestrator loop

One cycle:

1. Refresh portfolio state and daily P&L via MCP read tools (kill-switch input).
2. Check the clock. If closed, journal a no-op and exit.
3. Manage exits on open positions first — **spread-level**, per §3.2.
4. For each underlying: run the LLM loop with a turn cap (5–8) and a wall-clock timeout.
5. On `propose_spread`: exit the loop, run the risk gate, journal the verdict with every
   rule's observed-vs-limit.
6. If approved: **the orchestrator**, not the model, calls the MCP write tool. Deterministic
   `client_order_id` — `sha1(ticker|expiry|short|long|date|cycle)` — so a crashed cycle
   cannot double-place on retry.
7. Journal the fill or the failure.

Flags: `--dry-run` (default; everything except the write tool call), `--live`, `--ticker`,
`--force` (bypass the market-hours gate for offline testing).

Failure handling that matters: if the model exhausts its turns without proposing, journal
`no_proposal` with its last message and move on — never retry into a loop. If the model
proposes something malformed, journal `malformed_proposal` and move on. Neither is an error
state; both are useful evidence and both belong on the dashboard.

> **Gate:** `--dry-run --force --ticker SPY` completes against live data, and the journal
> contains the model's tool calls, its rationale, the full nine-rule verdict, and no order.

### Stage 6 — Dashboard

Port the restyled dashboard. Add one panel the previous build had no need for: **Agent
Reasoning** — the model's rationale for the most recent proposal, the tools it called to get
there, and the gate's verdict beside it. Keep the degraded-mode behaviour and the orphan-branch
journal publishing.

> **Gate:** renders with live data, renders with credentials stubbed out, zero exceptions in
> both. Check it at 1440px and on an actual phone.

### Stage 7 — Scheduling

Cron, every 5 minutes, market hours gated **in Python via the MCP clock tool**, not in the
crontab — the box is PKT and the market is ET. Pin `CRON_TZ` as belt-and-braces. Install in
`--dry-run`; flipping to `--live` is a deliberate, separate act.

Verify the journal-push script works in a **bare cron environment** (`env -i`), not just in
an interactive shell. A silent credential failure there freezes the deployed dashboard at
its seeded state and nobody notices until a judge does.

> **Gate:** `env -i HOME=$HOME PATH=/usr/bin:/bin bash -c 'cd <repo> && ./scripts/push_journal.sh'`
> exits 0, and `logs/cron.log` shows a "market closed, skipping" line within 5 minutes of install.

### Stage 8 — Submission materials

- `docs/WRITEUP.md` — one page. Three sections: **AI logic** (the model's role, the tool
  loop, provider), **risk gates** (nine rules, thresholds, why the model can't reach the
  write tools), **Alpaca infrastructure** (MCP server, which tools, paper environment).
- `demo/video_script.md` — 3 min, shot list, paste-ready commands. Centre it on the
  rejection moment.
- `demo/slides.md` — 5 slides.
- `demo/social_posts.md` — 5 drafts, correct handles.
- `SUBMISSION.md` — account ID, repo URL, dashboard URL, video URL, post links.
- `README.md` — strategy, architecture diagram, run instructions.

---

## 5. Journal schema additions

The previous journal recorded decisions. This one must also record **reasoning**, because
that is the evidence that an AI agent exists. New event types:

- `agent_turn` — one model turn: which tool it called, with what arguments, and the result size
- `agent_proposal` — the `propose_spread` payload including the full rationale text
- `agent_no_proposal` — turn cap or timeout hit, with the model's last message
- `gate_verdict` — every rule with `passed`, `observed`, `limit`, plus the final decision and,
  when sized down, the requested vs approved contract counts
- `cycle_summary` — extended with `provider`, `model`, `turns_used`, `mode`

Keep the schema additive so the ported dashboard and analytics keep working.

---

## 6. Go-live

Create the **fresh** paper account only when the code is verified — not before, so the judged
account's history contains nothing but judged trading. Confirm $100,000 balance and options
level 3. Point `.env` at the new keys. Run one `--live` cycle under supervision and confirm
the order appears in Alpaca's own dashboard. Then flip cron to `--live`.

Daily thereafter: check the dashboard, check `logs/cron.log` for errors, and check that the
`data`-branch timestamp is still advancing. **That timestamp is the canary** — if it stops
moving during market hours the journal push has broken even though it worked on install day.

---

## 7. Sequencing note

The existing repo is roughly two hours from compliance: swap its `alpaca-py` calls for
Alpaca CLI invocations with JSON output. Alpaca's own page describes the CLI as *"built for
long-running agent sessions, cron jobs and CI, where MCP is heavier than needed"* — which
describes that architecture exactly, so it is the sanctioned choice there rather than a
consolation prize.

Doing that swap first buys a guaranteed-compliant submission before spending the larger
budget on this build. If time or credits run out mid-way through the MCP build, you still
have something to submit. That is cheap insurance and the order is yours to choose.
