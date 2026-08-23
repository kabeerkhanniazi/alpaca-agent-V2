# Submission

> **Status: not yet submitted.** The five `TODO` fields below need filling before
> this is complete. Everything above them is done and verified.

---

## Links

| | |
|---|---|
| Repository | https://github.com/kabeerkhanniazi/alpaca-agent-V2 |
| Dashboard | `TODO` — Streamlit Cloud, tracks `main`, reads the `data` branch |
| Demo video | `TODO` — 3 min, script in `demo/video_script.md` |
| Judged account ID | `TODO` — fresh paper account, created immediately before go-live |
| Social posts | `TODO` — 5 drafts ready in `demo/social_posts.md` |

---

## Compliance

| Requirement | Where satisfied | Status |
|---|---|---|
| Autonomous AI trading agent using Alpaca's Trading API | `agent/orchestrator.py` — an LLM drives a multi-turn tool-calling loop each cycle, unattended via cron | ✅ |
| **Must use Alpaca's MCP server or CLI tools** | All Alpaca I/O goes through the MCP server. `alpaca-py` is not imported anywhere in the agent path, enforced by `tests/test_no_sdk_import.py` | ✅ |
| All strategies must incorporate options trading | Only instrument traded is a vertical credit spread on SPY/QQQ/IWM. No equity legs | ✅ |
| Starting balance $100,000 | Config asserts NAV ≈ 100000 on the first live cycle and warns otherwise | ⏳ pending fresh account |
| Brand-new paper account for judging | Created immediately before go-live so its history contains nothing but judged trading | ⏳ |
| One-page write-up: AI logic, risk gates, Alpaca infrastructure | `docs/WRITEUP.md` — three sections matching those three words | ✅ |
| Public repo + hosted dashboard | Local repo committed; needs a remote and a deploy | ⏳ |
| Up to 5 social posts tagging @lablabai / @AlpacaHQ | `demo/social_posts.md` — 5 drafts, correct handles | ⏳ drafted, not posted |

---

## What is verified

- **439 tests pass** (424 fully offline, no credentials needed).
- **Live cycle end to end**: `cron_runner.py --dry-run --force --ticker SPY`
  completes against real market data — 724 contracts fetched, 21 candidates in
  the delta window, model proposed in 4 turns, nine-rule verdict recorded, no
  order placed.
- **The rejection path**: tightening Rule 1 to 0.05 produced
  `[FAIL] R1_delta observed 0.1517 limit 0.05 → REJECTED` with every other rule
  still evaluated, and no order.
- **The mediation**: 11 write tools live in the process, 0 reachable by the
  model, and each refused at the call site as well as absent from its tool list.
- **Order convention**: a multi-leg credit spread was submitted to Alpaca with
  `limit_price: "-5"`, accepted, then cancelled. The sign convention is
  confirmed against the real API rather than assumed.
- **Bare cron**: the agent runs under `env -i` with only `HOME` and `PATH`,
  connects to the MCP server, and no-ops correctly when the market is closed.
- **Dashboard** renders with live data and with credentials stubbed out, zero
  exceptions in both.

## What is not done

1. **Nothing pushed yet.** The `origin` remote is configured
   (https://github.com/kabeerkhanniazi/alpaca-agent-V2) but this machine has no stored git
   credentials, so `main` still needs an interactive `git push`. Until that
   happens `scripts/push_journal.sh` exits 128 and a deployed dashboard would
   freeze at its seeded state.
2. **No fresh judged account.** Deliberate: `PLAN.md` §6 says create it only
   after the code is verified, so its history contains nothing but judged
   trading. Currently pointed at the dev paper account (`PA3DBILW1YRG`).
3. **Never run `--live`.** One supervised live cycle should happen before cron is
   flipped.
4. **OpenRouter key is free-tier.** The agent's shape is up to 24 model calls per
   cycle; at a 5-minute cadence that exceeds free daily caps well before a
   session ends. Failover to Groq absorbs it and is journalled, so nothing
   breaks — but most cycles would then be Groq-driven. Levers: raise
   `cycle_interval_seconds`, lower `max_turns_per_ticker`, or add credits.

---

## Go-live checklist

1. `git push -u origin main` (remote already configured; needs your credentials).
2. Verify `scripts/push_journal.sh` exits 0 now that `origin` is reachable.
3. Deploy `streamlit_app.py` to Streamlit Cloud tracking `main`.
4. Create the **fresh** paper account. Confirm $100,000 and options level 3.
   Point `.env` at the new keys. Record the account ID above.
5. Run one supervised `--live` cycle. Confirm the order appears in Alpaca's own
   dashboard.
6. `./install_cron.sh --live`.
7. Record the demo video (`demo/video_script.md`).
8. Post the five social drafts; record the links above.

**Daily thereafter:** check the dashboard, check `logs/cron.log` for errors, and
check that the `data` branch timestamp is still advancing. **That timestamp is
the canary** — if it stops moving during market hours the journal push has broken
even though it worked on install day.
