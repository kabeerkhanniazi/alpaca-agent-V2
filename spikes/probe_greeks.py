"""Determine whether Greeks/IV are actually returned, and on which feed."""
import asyncio, json, os, datetime as dt
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
load_dotenv()

PARAMS = StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env={
    "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
    "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
    "ALPACA_PAPER_TRADE": "true",
    "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]})

def data(res):
    txt = [c.text for c in res.content if getattr(c, "type", None) == "text"]
    if not txt:
        return {"_no_text": True, "is_error": res.is_error}
    try:
        return json.loads(txt[0]).get("data", json.loads(txt[0]))
    except Exception:
        return {"_raw": txt[0][:500]}

async def main():
    out = {}
    async with stdio_client(PARAMS) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            spot_res = await s.call_tool("get_stock_latest_trade", {"symbols": "SPY"})
            spot_d = data(spot_res)
            print("SPY latest trade:", json.dumps(spot_d)[:400])
            try:
                spot = spot_d["trades"]["SPY"]["p"]
            except Exception:
                spot = None
            print("spot =", spot)
            out["spot"] = spot_d

            if spot is None:
                return
            today = dt.date.today()
            base = {
                "underlying_symbol": "SPY",
                "type": "put",
                "expiration_date_gte": (today + dt.timedelta(days=7)).isoformat(),
                "expiration_date_lte": (today + dt.timedelta(days=14)).isoformat(),
                "strike_price_gte": round(spot * 0.90),
                "strike_price_lte": round(spot * 1.01),
                "limit": 40,
            }
            for feed in ["indicative", "opra"]:
                args = dict(base, feed=feed)
                d = data(await s.call_tool("get_option_chain", args))
                snaps = d.get("snapshots", {}) if isinstance(d, dict) else {}
                withg = [k for k, v in snaps.items() if "greeks" in v]
                print(f"\nfeed={feed}: {len(snaps)} contracts, {len(withg)} with greeks; err={d.get('_raw','')[:200]}")
                if withg:
                    k = withg[len(withg)//2]
                    print("SAMPLE", k, json.dumps(snaps[k], indent=2)[:1200])
                out[f"chain_{feed}"] = {"args": args, "data": d}

            # single-contract snapshot path
            snaps = out.get("chain_indicative", {}).get("data", {}).get("snapshots", {})
            if snaps:
                sym = sorted(snaps)[len(snaps)//2]
                d = data(await s.call_tool("get_option_snapshot", {"symbols": sym, "feed": "indicative"}))
                print(f"\nget_option_snapshot({sym}):", json.dumps(d)[:900])
                out["snapshot_single"] = d

    with open(os.path.join(os.path.dirname(__file__), "greeks_probe.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)

asyncio.run(main())
