# Social posts — 5 drafts

Handles: **X** → `@lablabai`, `@AlpacaHQ` · **LinkedIn** → lablab.ai, Alpaca

Fill `<repo>`, `<dashboard>`, `<video>` before posting. Attach the media noted.

---

## 1 — X · the hook

> Most "AI trading agents" hand the model the broker API and hope.
>
> Mine can't place a trade. Not blocked — it was never given a tool that can.
>
> Alpaca's MCP server exposes 74 tools. 17 write. The model sees 11 reads and one
> synthetic `propose_spread` that places nothing.
>
> Built for @lablabai × @AlpacaHQ 🦙
>
> <repo>

*Media: the architecture diagram.*

---

## 2 — X · the rejection

> The best 30 seconds of my demo is the agent *not* trading.
>
> Model: "SPY 750/745, IV rank 37.7%, short delta -0.1517, $53 credit."
> Gate: `[FAIL] R1_delta observed 0.1517 limit 0.05 → REJECTED`
>
> No order. Nine rules evaluated, every one journalled with observed vs limit.
>
> @AlpacaHQ @lablabai
>
> <video>

*Media: terminal screenshot of the nine-rule verdict.*

---

## 3 — X · the finding

> Spent a while on @AlpacaHQ's MCP server this week. Two things worth knowing:
>
> 1. `ALPACA_TOOLSETS` scoping works, but `trading` bundles `get_all_positions`
>    with `place_option_order` — you can't get position reads without order
>    placement. Allowlist in code.
>
> 2. Options tools default to `feed="opra"`. On a free account that's a 403, not
>    a fallback. Pass `indicative` explicitly.
>
> @lablabai

*No media — this one earns attention by being useful.*

---

## 4 — LinkedIn · the build

> **What stops an AI trading agent when it's wrong?**
>
> That question shaped everything I built for the lablab.ai × Alpaca hackathon.
>
> The usual approach hands a language model the broker API. Mine splits the
> problem: the model reasons over live market data through Alpaca's MCP server
> and *proposes* vertical credit spreads. A deterministic nine-rule risk gate —
> which the model cannot see, call or influence — decides whether any proposal
> becomes an order.
>
> The model receives 11 read-only tools plus one synthetic `propose_spread` that
> places nothing. Of the 17 write-capable tools on the server, zero are reachable
> by it. There's no prompt injection that reaches an order, because the
> capability was never handed over.
>
> Two tests exist purely to fail the build if anyone ever changes that.
>
> What surprised me: Alpaca's server labels its own tool output as
> `untrusted_tool_output` with an instruction to treat it as data, not
> instructions. Prompt-injection hygiene at the transport layer. Nice touch.
>
> Repo, dashboard and a 3-minute demo below.
>
> #AI #Trading #MCP #Alpaca #lablabai

*Media: architecture diagram + dashboard screenshot.*

---

## 5 — LinkedIn · the honest one

> Things I got wrong building an autonomous options agent, so you don't have to:
>
> **The delta window isn't a fixed distance from spot.** I bracketed the option
> chain by strike range. Worked fine — until IV moved and the bracket returned
> nothing. Now it's derived from a percentage of spot, and the logs distinguish
> "no strike existed in the window" from "my bracket was too narrow." Different
> problems, opposite fixes.
>
> **Alpaca reports spread legs as separate positions.** Judge exits per leg and a
> profitable short put gets closed on its own, orphaning the long wing — turning
> a defined-risk spread into a naked position. Group before you evaluate.
>
> **A model picks strikes from a chain it saw several turns ago.** Quotes move.
> Without a validation step, a stale strike surfaces as a confusing credit
> failure that points at the price instead of the strike. Re-quote before the
> gate.
>
> **Free-tier defaults bite.** The options tools default to a feed my account
> can't use. It 403s rather than falling back. One word.
>
> Built for the lablab.ai × Alpaca hackathon. Full write-up in the repo.
>
> #BuildInPublic #AI #Trading #Alpaca #lablabai
