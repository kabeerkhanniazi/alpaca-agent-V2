"""Stage 2 gate: round-trip a two-tool schema through a real provider.

Sends a toy two-tool schema, executes whichever tool the model asks for, feeds
the result back, and requires a final answer that actually uses it. That is the
exact loop the orchestrator will run, so a provider that passes here can drive
a cycle and one that fails here would fail at cycle 4 instead — which is a far
worse place to find out.

    python spikes/gate_stage2.py                  # test the configured chain
    python spikes/gate_stage2.py --survey         # rank the free OpenRouter models
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from agent.llm import LLMClient, LLMError, ToolCall  # noqa: E402

# Two tools, deliberately: with one, a model that always calls the only tool it
# has looks correct. Picking the right one of two is the actual skill under test.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_account_balance",
            "description": "Get the account's current cash balance in USD.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_option_quote",
            "description": "Get the current bid and ask for one option contract.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "OCC option symbol, e.g. SPY260831P00752000",
                    }
                },
                "required": ["symbol"],
            },
        },
    },
]

FAKE_RESULTS = {
    "get_account_balance": {"cash": 100000, "currency": "USD"},
    "get_option_quote": {"symbol": "SPY260831P00752000", "bid": 1.56, "ask": 1.57},
}

PROMPT = (
    "What is the bid price of the option contract SPY260831P00752000? "
    "Use the tools available to you, then state the bid price."
)


def execute(call: ToolCall) -> str:
    """Stand-in for the MCP layer: return canned JSON for a known tool."""
    return json.dumps(FAKE_RESULTS.get(call.name, {"error": f"unknown tool {call.name}"}))


async def round_trip(client: LLMClient, http: httpx.AsyncClient, max_turns: int = 6) -> dict:
    """Run the loop and report what happened at each stage."""
    messages: list[dict] = [
        {"role": "system", "content": "You are a terse assistant. Use tools when they help."},
        {"role": "user", "content": PROMPT},
    ]
    report = {
        "called_a_tool": False,
        "called_the_right_tool": False,
        "passed_correct_args": False,
        "used_the_result": False,
        "turns": 0,
        "provider": "",
        "model": "",
        "switched_from": None,
        "final_text": "",
        "error": None,
    }

    for turn in range(1, max_turns + 1):
        report["turns"] = turn
        result = await client.chat(messages, TOOLS, client=http)
        report["provider"], report["model"] = result.provider, result.model
        report["switched_from"] = result.switched_from

        if not result.wants_tools:
            report["final_text"] = result.text
            # The canned bid is 1.56; a model that reports it has demonstrably
            # read the tool result rather than inventing a plausible number.
            report["used_the_result"] = "1.56" in result.text
            return report

        report["called_a_tool"] = True
        messages.append({
            "role": "assistant",
            "content": result.text or None,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in result.tool_calls
            ],
        })

        for call in result.tool_calls:
            if call.name == "get_option_quote":
                report["called_the_right_tool"] = True
                if call.arguments.get("symbol") == "SPY260831P00752000":
                    report["passed_correct_args"] = True
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": execute(call),
            })

    report["error"] = f"hit the {max_turns}-turn cap without a final answer"
    return report


def verdict(r: dict) -> tuple[bool, str]:
    if r.get("error"):
        return False, r["error"]
    checks = [
        ("called a tool", r["called_a_tool"]),
        ("picked the right tool", r["called_the_right_tool"]),
        ("passed correct arguments", r["passed_correct_args"]),
        ("used the tool result", r["used_the_result"]),
    ]
    failed = [name for name, ok in checks if not ok]
    return (not failed), ("all checks passed" if not failed else "failed: " + ", ".join(failed))


async def test_chain(chain: list[tuple[str, str]], label: str) -> bool:
    client = LLMClient(chain)
    print(f"\n{'=' * 78}\n{label}\n  chain: {client.describe()}\n{'=' * 78}")
    async with httpx.AsyncClient() as http:
        try:
            report = await round_trip(client, http)
        except LLMError as exc:
            print(f"  FAILED — {exc}")
            return False

    ok, reason = verdict(report)
    print(f"  served by:  {report['provider']}:{report['model']}")
    if report["switched_from"]:
        print(f"  failover:   from {report['switched_from']}")
    print(f"  turns:      {report['turns']}")
    print(f"  final text: {report['final_text'][:180]!r}")
    print(f"  {'PASS' if ok else 'FAIL'} — {reason}")
    return ok


async def survey() -> None:
    """Rank the free OpenRouter models by whether they can actually do this."""
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            timeout=60,
        )
        models = [
            m["id"] for m in resp.json()["data"]
            if m["id"].endswith(":free") and "tools" in (m.get("supported_parameters") or [])
        ]

    print(f"Surveying {len(models)} free tool-calling models on OpenRouter.\n")
    passed, failed = [], []
    for model in sorted(models):
        client = LLMClient([("openrouter", model)])
        async with httpx.AsyncClient() as http:
            try:
                report = await asyncio.wait_for(round_trip(client, http), timeout=180)
                ok, reason = verdict(report)
            except (LLMError, asyncio.TimeoutError) as exc:
                ok, reason = False, f"{type(exc).__name__}: {str(exc)[:110]}"
        (passed if ok else failed).append(model)
        print(f"  [{'PASS' if ok else 'FAIL'}] {model:<52} {reason[:100]}")

    print(f"\n{len(passed)}/{len(models)} passed.")
    if passed:
        print("Usable as primary:")
        for m in passed:
            print(f"  - {m}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", action="store_true",
                        help="Rank every free tool-calling model on OpenRouter.")
    args = parser.parse_args()

    if args.survey:
        await survey()
        return 0

    config = json.loads((Path(__file__).parent.parent / "config" / "agent_config.json").read_text())
    llm = config["llm"]

    primary_ok = await test_chain([(llm["provider"], llm["model"])], "PRIMARY")
    fallback = (llm.get("fallbacks") or [None])[0]
    fallback_ok = await test_chain(
        [(fallback["provider"], fallback["model"])], "FALLBACK"
    ) if fallback else False

    print(f"\n{'=' * 78}")
    print(f"Stage 2 gate: primary {'PASS' if primary_ok else 'FAIL'}, "
          f"fallback {'PASS' if fallback_ok else 'FAIL'}")
    return 0 if (primary_ok and fallback_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
