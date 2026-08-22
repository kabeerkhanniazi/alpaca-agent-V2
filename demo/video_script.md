# Demo video — 3 minutes

Centre of gravity: **the rejection**. Judges have seen LLMs place trades. They
have not seen one credibly prevented from doing so.

Total 3:00. Times are cumulative.

---

## 0:00–0:20 — The claim

**On screen:** the architecture diagram from README.md.

> "This is an options agent where a language model does the analysis and a
> deterministic gate does the deciding. The model reads live Alpaca data through
> Alpaca's MCP server, and proposes trades. It cannot place them. Not because
> it's told not to — because it was never given a tool that can."

---

## 0:20–0:50 — Prove the mediation

**Terminal:**

```bash
python spikes/gate_stage3.py
```

**Point at these two lines in the output:**

```
write tools present in the process: 11  ['cancel_all_orders', 'close_position', 'place_option_order', ...]
write tools in the model's list:     0  []
```

> "The server exposes 74 tools. Eleven write tools are live in this process right
> now. The model is handed twelve tools, and none of them is one of those. It
> also can't call one by name — watch."

**Same output, the refusals:**

```
call_read('place_option_order') -> refused
call_read('close_all_positions') -> refused
```

---

## 0:50–1:40 — A real cycle

**Terminal:**

```bash
python cron_runner.py --dry-run --force --ticker SPY
```

Let it run. While it does:

> "It's checking the clock, the account, open positions, then pulling the SPY
> chain — seven hundred contracts, filtered to twenty-one that sit in the delta
> window we want."

**When it finishes, show the journal:**

> "Four turns. Here's what it decided, in its own words."

**Read the rationale on screen** — it cites spot, IV rank, the delta of the chosen
strike, the credit, the width, DTE, and existing exposure.

> "That's the evidence a model actually reasoned. Every number in it came from a
> tool call in this cycle."

---

## 1:40–2:30 — The rejection *(the shot that matters)*

**Tighten one threshold on camera:**

```bash
# config/risk_config.json — delta.max_abs: 0.20 -> 0.05
python cron_runner.py --dry-run --force --ticker SPY
```

**Show the gate output:**

```
[FAIL] R1_delta      observed 0.1517   limit 0.05
[PASS] R2_notional   observed 924.0    limit 2000.0
...
REJECTED — R1_delta: Short strike delta 0.1517 against a 0.05 cap.

ORDER EVENTS: NONE — no order was placed
```

> "The model proposed a perfectly sensible trade. The gate said no, named the
> rule, showed the observed value against the limit — and no order exists. Every
> other rule still ran, so the journal has the whole picture, not just the first
> objection."

**Beat. Let that sit.**

> "The model never sees this gate. It can't argue with it, can't route around it,
> and can't be talked into bypassing it, because the thing it would need to
> bypass it was never in its hands."

---

## 2:30–3:00 — Close

**Dashboard, Agent Reasoning panel:**

> "This panel is the reasoning and the verdict side by side, per underlying, for
> every cycle. Provider, model, turns used. When the model declines because
> conditions are poor, that renders as healthy — because it is. When it runs out
> of turns, that renders as a defect, because that one's on me to fix."

**Final frame — the numbers:**

> "Alpaca's MCP server, 74 tools, 11 of them write, zero reachable by the model.
> Nine risk rules. 433 tests, two of which exist purely to fail the build if
> anyone ever hands the model a write tool."

---

## Shot list

| Shot | Source | Note |
|---|---|---|
| Architecture diagram | `README.md` | Hold 8s, don't narrate every box |
| Mediation proof | `python spikes/gate_stage3.py` | Pre-run; splice for pace |
| Live cycle | `cron_runner.py --dry-run --force --ticker SPY` | ~75s real; cut the middle |
| Rationale | `data/journal.jsonl` → `agent_proposal` | Format it large and readable |
| **Rejection** | Tighten `delta.max_abs` to 0.05, rerun | **Do not cut this one short** |
| Dashboard | `streamlit run streamlit_app.py` | Agent Reasoning panel |

## Before recording

- `git checkout config/risk_config.json` — restore 0.20 after the demo
- Clear `data/journal.jsonl` so the cycle you record is the one on screen
- Terminal at 16pt+; the observed-vs-limit column is the whole point
- The free-tier model can be slow — pre-run once so the cache is warm
