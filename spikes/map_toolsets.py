"""Map each ALPACA_TOOLSETS value to the exact tools it enables."""
import asyncio, json, os
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
load_dotenv()

TOOLSETS = ["account","trading","watchlists","assets","stock-data","crypto-data",
            "options-data","corporate-actions","news","fixed-income-data","index-data"]

def params(ts):
    env={"ALPACA_API_KEY":os.environ["ALPACA_API_KEY"],
         "ALPACA_SECRET_KEY":os.environ["ALPACA_SECRET_KEY"],
         "ALPACA_PAPER_TRADE":"true","PATH":os.environ["PATH"],"HOME":os.environ["HOME"]}
    if ts: env["ALPACA_TOOLSETS"]=ts
    return StdioServerParameters(command="uvx",args=["alpaca-mcp-server"],env=env)

async def names(ts):
    async with stdio_client(params(ts)) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            return sorted(t.name for t in (await s.list_tools()).tools)

async def main():
    out={}
    for ts in TOOLSETS:
        try:
            out[ts]=await names(ts)
            print(f"{ts}: {len(out[ts])}")
        except Exception as e:
            out[ts]={"error":str(e)[:200]}
            print(f"{ts}: ERROR {str(e)[:120]}")
    # baseline: tools present under every toolset = always-on
    sets=[set(v) for v in out.values() if isinstance(v,list)]
    always=set.intersection(*sets) if sets else set()
    out["_always_on"]=sorted(always)
    print("\nalways-on regardless of toolset:",sorted(always))
    for ts in TOOLSETS:
        if isinstance(out.get(ts),list):
            out[ts+"_exclusive"]=sorted(set(out[ts])-always)
    with open(os.path.join(os.path.dirname(__file__),"toolset_map.json"),"w") as f:
        json.dump(out,f,indent=2)

asyncio.run(main())
