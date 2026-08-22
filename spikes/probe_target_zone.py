"""Confirm the -0.20..-0.15 delta window is reachable in the 7-14 DTE band."""
import asyncio, json, os, datetime as dt
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
load_dotenv()
PARAMS = StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env={
    "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
    "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
    "ALPACA_PAPER_TRADE": "true", "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]})

def data(res):
    txt=[c.text for c in res.content if getattr(c,"type",None)=="text"]
    return json.loads(txt[0]).get("data") if txt else None

async def main():
    spot=765.55
    async with stdio_client(PARAMS) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            # what expiries exist in the 7-14 DTE band?
            today=dt.date.today()
            contracts=data(await s.call_tool("get_option_contracts",{
                "underlying_symbols":"SPY","type":"put",
                "expiration_date_gte":(today+dt.timedelta(days=7)).isoformat(),
                "expiration_date_lte":(today+dt.timedelta(days=14)).isoformat(),
                "limit":10000}))
            exps=sorted({c["expiration_date"] for c in contracts.get("option_contracts",[])})
            print("expiries in 7-14 DTE band:",exps)

            out={"expiries":exps,"chains":{}}
            for exp in exps:
                d=data(await s.call_tool("get_option_chain",{
                    "underlying_symbol":"SPY","type":"put","feed":"indicative",
                    "expiration_date":exp,
                    "strike_price_gte":round(spot*0.955),
                    "strike_price_lte":round(spot*1.0),
                    "limit":200}))
                snaps=d.get("snapshots",{})
                rows=[]
                for k,v in snaps.items():
                    g=v.get("greeks")
                    if not g: continue
                    q=v.get("latestQuote",{})
                    rows.append((int(k[-8:])/1000,g["delta"],v.get("impliedVolatility"),q.get("bp"),q.get("ap"),k))
                rows.sort()
                tgt=[r for r in rows if -0.20<=r[1]<=-0.15]
                dte=(dt.date.fromisoformat(exp)-today).days
                print(f"\n{exp} (DTE={dte}): {len(rows)} contracts w/ greeks, {len(tgt)} in target delta window")
                for r in tgt: print(f"   strike {r[0]:.0f} delta {r[1]:+.4f} IV {r[2]:.4f} bid {r[3]} ask {r[4]} {r[5]}")
                out["chains"][exp]={"dte":dte,"rows":rows,"target":tgt}
    with open(os.path.join(os.path.dirname(__file__),"target_zone.json"),"w") as f:
        json.dump(out,f,indent=2,default=str)

asyncio.run(main())
