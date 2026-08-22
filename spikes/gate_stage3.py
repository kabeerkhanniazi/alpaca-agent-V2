"""Stage 3 gate: show what the model can and cannot reach, against the live server.

Connects for real, prints the exact tool list the model would be handed, and
demonstrates that the write tools are present in the process yet unreachable
through the model's path.

    python spikes/gate_stage3.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from agent.mcp_client import MCPClient, ToolNotAllowed  # noqa: E402
from agent.tools import PROPOSE_SPREAD  # noqa: E402


async def main() -> int:
    async with MCPClient() as mcp:
        server_tools = set(mcp._server_tools)
        model_tools = [s["function"]["name"] for s in mcp.tools_for_model()]

        print("=" * 74)
        print("WHAT THE SERVER EXPOSES TO THE PROCESS")
        print("=" * 74)
        print(f"  toolsets:            {mcp.config['toolsets']}")
        print(f"  tools in process:    {len(server_tools)}")
        writes_present = sorted(server_tools & mcp.write_tools)
        print(f"  write tools present: {len(writes_present)}  {writes_present}")

        print()
        print("=" * 74)
        print("WHAT THE MODEL IS HANDED")
        print("=" * 74)
        for name in model_tools:
            marker = "  (synthetic)" if name == PROPOSE_SPREAD else ""
            print(f"  {name}{marker}")
        print(f"\n  total: {len(model_tools)} tools")

        leaked = set(model_tools) & mcp.write_tools
        print(f"  write tools in the model's list: {len(leaked)}  {sorted(leaked)}")

        missing = mcp.missing_read_tools()
        if missing:
            print(f"\n  WARNING — allowlisted but absent from the server: {missing}")

        print()
        print("=" * 74)
        print("THE SECOND DOOR — the call site refuses what the schema list omits")
        print("=" * 74)
        for attempt in ("place_option_order", "close_all_positions", "create_locate"):
            try:
                await mcp.call_read(attempt, {})
                print(f"  {attempt}: NOT REFUSED — this is a critical failure")
                return 1
            except ToolNotAllowed:
                print(f"  call_read({attempt!r}) -> refused")

        print()
        print("=" * 74)
        print("A REAL READ THROUGH THE MEDIATION LAYER")
        print("=" * 74)
        clock = await mcp.call_read("get_clock", {})
        print(f"  get_clock -> {json.dumps(clock)}")
        assert "_alpaca_mcp_security" not in json.dumps(clock), "envelope was not stripped"
        print("  security envelope stripped: yes")

        account = await mcp.call_read("get_account_info", {})
        print(f"  get_account_info -> equity={account['equity']} "
              f"options_level={account['options_trading_level']}")

        print()
        print("=" * 74)
        ok = not leaked and not missing
        print(f"Stage 3 gate: {'PASS' if ok else 'FAIL'} — "
              f"{len(writes_present)} write tools in the process, "
              f"{len(leaked)} reachable by the model")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
