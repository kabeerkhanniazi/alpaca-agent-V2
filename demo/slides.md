# Slides — 5

---

## 1. The problem with "AI trading agent"

Hand a model the broker API and it will trade.
That is not autonomy — that is an unguarded loop with a language model in it.

The interesting question is not *can a model trade*.
It is **what stops it when it is wrong**.

> Judges have seen LLMs place trades.
> They have not seen one credibly prevented from doing so.

---

## 2. The split

```
LLM ANALYST ──► 11 read tools + propose_spread
                         │
                         ▼
              LEG VALIDATION ──► RISK GATE (9 rules)
                                   │        │
                              APPROVE    REJECT
                                   │        │
                          ORCHESTRATOR   journal only
                          calls the write tool
```

`propose_spread` is **not an Alpaca tool**. Calling it places nothing — it ends
the model's turn and returns control to deterministic code.

| Tools the MCP server exposes | 74 |
| Write tools live in the process | 11 |
| **Tools the model receives** | **12** |
| **Write tools the model can reach** | **0** |

Not disabled. Not discouraged. **Absent.**

---

## 3. Why configuration alone could not do this

Alpaca's `ALPACA_TOOLSETS` lets you scope the server. We use it — 74 tools down
to 53. But it **cannot** express the split this design needs:

> The `trading` toolset bundles `get_all_positions` — which the agent needs for
> portfolio state, the kill switch and exits — together with
> `place_option_order`, `close_all_positions`, `cancel_all_orders` and six more.

There is no value of that variable that yields position reads without order
placement. So the allowlist lives in code, by **inclusion**, and is the thing
under test.

A subtractive allowlist would have missed `create_locate` — a write tool
belonging to no documented toolset at all.

---

## 4. The rejection

```
Model:  SPY 750/745 put spread, 9 DTE, $53 credit.
        "IV rank 37.7%, short delta -0.1517, no existing SPY exposure."

Gate:   [FAIL] R1_delta      observed 0.1517   limit 0.05
        [PASS] R2_notional   observed 924.0    limit 2000.0
        [PASS] R4_credit     observed 38.0     limit 25.0
        ... all nine evaluated, not just the first failure

        REJECTED — no order placed.
```

Every rule returns `{rule, passed, observed, limit}`, so a rejection is always
explainable without re-deriving anything.

The gate also **sizes down** rather than rejecting outright: the model asked for
5 contracts at 2.1% of NAV; Rule 2 approved 4.

---

## 5. Built on Alpaca, honestly

- **Alpaca MCP server** v3.4.7 over stdio — every call, no exceptions.
  `alpaca-py` is not imported anywhere in the agent path, and a test fails the
  build if it ever is.
- **Free-tier feeds**: `iex` bars, `indicative` options. OPRA needs a signed
  agreement; we say so rather than implying otherwise.
- **Verified, not assumed**: a multi-leg credit submits as a *negative* limit
  price — confirmed by an order Alpaca accepted, then cancelled.
- **433 tests.** Two exist purely to fail the build if anyone hands the model a
  write tool or reintroduces the SDK.

The model that drove the trading is named in the journal on every cycle.
