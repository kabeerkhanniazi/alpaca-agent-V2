"""Stage 1 spike: connect to the Alpaca MCP server over stdio and enumerate every tool.

Writes the full tool inventory (name, description, inputSchema) to spikes/tools_raw.json.
Nothing here is production code; the adapter is written against what this captures.
"""
import asyncio, json, os, sys
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

SERVER_ENV = {
    "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
    "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
    "ALPACA_PAPER_TRADE": os.getenv("ALPACA_PAPER_TRADE", "true"),
    "PATH": os.environ["PATH"],
    "HOME": os.environ["HOME"],
}

def server_params(toolsets: str | None = None) -> StdioServerParameters:
    env = dict(SERVER_ENV)
    if toolsets is not None:
        env["ALPACA_TOOLSETS"] = toolsets
    return StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env=env)

async def main() -> None:
    toolsets = sys.argv[1] if len(sys.argv) > 1 else None
    async with stdio_client(server_params(toolsets)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server: {init.server_info.name} v{init.server_info.version}", file=sys.stderr)

            tools = (await session.list_tools()).tools
            print(f"tool count: {len(tools)}", file=sys.stderr)

            payload = {
                "server_name": init.server_info.name,
                "server_version": init.server_info.version,
                "toolsets_env": toolsets,
                "tool_count": len(tools),
                "tools": [
                    {
                        "name": t.name,
                        "description": (t.description or "").strip(),
                        "inputSchema": t.input_schema,
                    }
                    for t in sorted(tools, key=lambda x: x.name)
                ],
            }
            out = os.path.join(os.path.dirname(__file__), "tools_raw.json")
            if toolsets:
                out = out.replace(".json", f"_{toolsets.replace(',', '-')}.json")
            with open(out, "w") as f:
                json.dump(payload, f, indent=2)
            print(f"wrote {out}", file=sys.stderr)
            for t in payload["tools"]:
                print(t["name"])

asyncio.run(main())
