"""★ The safety test: no write-capable tool ever reaches the model.

This is the most important test in the suite. It encodes the architecture in
PLAN.md section 1 — the model reasons over read tools and proposes; only
deterministic code can execute. If this test is deleted or weakened, the build
has regressed regardless of what else passes.

It runs offline against a fake server that offers the full 74-tool surface the
real one exposes, so the assertions hold without credentials or a network.
`test_live_server_exposes_no_write_tools_to_the_model` repeats the check against
the real server and is skipped when credentials are absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.mcp_client import MCPClient, ToolNotAllowed, load_allowlist, unwrap
from agent.tools import PROPOSE_SPREAD

ALLOWLIST = load_allowlist()

# The complete tool inventory of Alpaca MCP Server v3.4.7, enumerated live in
# Stage 1 and recorded in docs/mcp_tools.md. Held here as a literal so the test
# keeps testing the real surface even when the server is unreachable.
ALL_SERVER_TOOLS = sorted(set(ALLOWLIST["read_tools"]) | set(ALLOWLIST["write_tools"]) | {
    "get_account_activities", "get_account_activities_by_type", "get_account_config",
    "get_portfolio_history", "get_all_assets", "get_asset", "get_calendar",
    "get_corporate_action_announcement", "get_corporate_action_announcements",
    "get_corporate_actions", "get_crypto_bars", "get_crypto_latest_bar",
    "get_crypto_latest_orderbook", "get_crypto_latest_quote", "get_crypto_latest_trade",
    "get_crypto_quotes", "get_crypto_snapshot", "get_crypto_trades",
    "get_fixed_income_latest_quotes", "get_index_latest_values", "get_index_values",
    "get_locate", "get_locate_quotes", "get_locates", "get_market_movers",
    "get_most_active_stocks", "get_news", "get_option_bars", "get_option_contract",
    "get_option_exchange_codes", "get_option_latest_quote", "get_option_latest_trade",
    "get_option_trades", "get_order_by_client_id", "get_order_by_id",
    "get_stock_latest_bar", "get_stock_latest_quote", "get_stock_quotes",
    "get_stock_trades", "get_watchlist_by_id", "get_watchlists",
    "fetch_alpaca_doc", "get_alpaca_endpoint_docs", "list_alpaca_api_endpoints",
    "search_alpaca_api_specs", "search_alpaca_docs",
})


class FakeTool:
    """Stands in for an mcp.types.Tool, using the same snake_case attributes."""

    def __init__(self, name: str):
        self.name = name
        self.description = f"Description of {name}."
        self.input_schema = {"type": "object", "properties": {}}


@pytest.fixture
def connected_client() -> MCPClient:
    """A client that believes it is connected to the full 74-tool server."""
    client = MCPClient()
    client._server_tools = {name: FakeTool(name) for name in ALL_SERVER_TOOLS}
    return client


def model_tool_names(client: MCPClient) -> set[str]:
    return {schema["function"]["name"] for schema in client.tools_for_model()}


# ------------------------------------------------------ ★ the core invariant


def test_no_write_tool_reaches_the_model(connected_client):
    """The single assertion this whole architecture rests on."""
    exposed = model_tool_names(connected_client)
    leaked = exposed & set(ALLOWLIST["write_tools"])

    assert not leaked, (
        f"WRITE TOOLS LEAKED TO THE MODEL: {sorted(leaked)}. "
        "The model must have no route to place, cancel, or close anything. "
        "Fix the allowlist — do not weaken this test."
    )


@pytest.mark.parametrize("write_tool", sorted(ALLOWLIST["write_tools"]))
def test_each_write_tool_individually_is_absent(connected_client, write_tool):
    """Named one by one, so a failure says which tool leaked."""
    assert write_tool not in model_tool_names(connected_client)


def test_the_order_placement_tool_is_absent_even_though_the_agent_uses_it(connected_client):
    """The orchestrator calls place_option_order. The model still never sees it.

    This is the distinction the whole design turns on: the capability exists in
    the process, but not in the model's reach.
    """
    assert "place_option_order" not in model_tool_names(connected_client)
    assert "place_option_order" in connected_client.orchestrator_write_tools


def test_the_model_gets_exactly_the_model_subset_plus_propose_spread(connected_client):
    assert model_tool_names(connected_client) == set(ALLOWLIST["model_tools"]) | {PROPOSE_SPREAD}


def test_the_model_subset_is_narrower_than_what_the_orchestrator_may_read():
    """Every schema is resent on every turn, so the list has a token cost.

    The tools left out are ones the orchestrator calls deterministically and the
    analyst has no use for. Narrowing it also narrows what the model can reach,
    so this is a security property as well as a budget one.
    """
    model = set(ALLOWLIST["model_tools"])
    read = set(ALLOWLIST["read_tools"])
    assert model < read, "the model list must be a strict subset of the read allowlist"
    assert not model & set(ALLOWLIST["write_tools"])


def test_the_model_still_gets_what_it_needs_to_do_the_job(connected_client):
    """Trimming must not remove the tools the analyst prompt tells it to use."""
    exposed = model_tool_names(connected_client)
    for essential in ("get_clock", "get_account_info", "get_all_positions",
                      "get_option_chain", PROPOSE_SPREAD):
        assert essential in exposed, f"the analyst cannot work without {essential}"


def test_a_tool_the_server_offers_but_the_allowlist_omits_is_not_exposed(connected_client):
    """The list is built by inclusion, so extra server tools cannot slip in.

    `create_locate` is the case that matters: it is a write tool belonging to no
    documented toolset, so a subtractive allowlist would have missed it.
    """
    exposed = model_tool_names(connected_client)
    assert "create_locate" not in exposed
    assert "get_news" not in exposed
    assert "search_alpaca_docs" not in exposed


def test_a_write_tool_added_to_the_server_later_still_does_not_leak(connected_client):
    """A future server release adding a write tool must not silently widen the surface."""
    connected_client._server_tools["place_futures_order"] = FakeTool("place_futures_order")
    assert "place_futures_order" not in model_tool_names(connected_client)


# --------------------------------------------------- the second door: calling


@pytest.mark.parametrize("write_tool", sorted(ALLOWLIST["write_tools"]))
async def test_call_read_refuses_every_write_tool(connected_client, write_tool):
    """Even if a write tool name were somehow requested, the call does not happen."""
    with pytest.raises(ToolNotAllowed):
        await connected_client.call_read(write_tool, {})


async def test_call_read_refuses_an_invented_tool_name(connected_client):
    with pytest.raises(ToolNotAllowed):
        await connected_client.call_read("place_order_but_sneaky", {})


async def test_call_read_refuses_to_dispatch_propose_spread(connected_client):
    """propose_spread is handled in-process; it is not an Alpaca tool."""
    with pytest.raises(ToolNotAllowed):
        await connected_client.call_read(PROPOSE_SPREAD, {})


async def test_call_write_refuses_tools_outside_the_orchestrator_set(connected_client):
    """The orchestrator's write door is narrow: two tools, not seventeen."""
    with pytest.raises(ToolNotAllowed):
        await connected_client.call_write("close_all_positions", {})
    with pytest.raises(ToolNotAllowed):
        await connected_client.call_write("cancel_all_orders", {})


# ------------------------------------------------------------ allowlist shape


def test_read_and_write_lists_do_not_overlap():
    assert not set(ALLOWLIST["read_tools"]) & set(ALLOWLIST["write_tools"])


def test_an_ambiguous_allowlist_is_rejected_at_construction():
    """A tool in both lists is a contradiction in the safety boundary."""
    bad = json.loads(json.dumps(ALLOWLIST))
    bad["read_tools"].append("place_option_order")
    with pytest.raises(ValueError, match="both read and write"):
        MCPClient(allowlist=bad)


def test_orchestrator_write_tools_are_a_subset_of_known_write_tools():
    assert set(ALLOWLIST["orchestrator_write_tools"]) <= set(ALLOWLIST["write_tools"])


def test_the_toolset_env_var_excludes_the_toolsets_we_do_not_need():
    """Defence in depth: fewer tools in the process than the default 74."""
    toolsets = set(ALLOWLIST["toolsets"].split(","))
    for excluded in ("watchlists", "crypto-data", "news", "fixed-income-data", "index-data"):
        assert excluded not in toolsets


def test_every_read_tool_is_actually_a_read():
    """Guards against a write tool being added to read_tools by name confusion."""
    for name in ALLOWLIST["read_tools"]:
        assert name.startswith("get_"), f"{name} does not look like a read tool"


# ------------------------------------------------------------ result unwrapping


def test_the_security_envelope_is_stripped_before_the_model_sees_it():
    """The envelope repeats on every result and would burn the 8-turn budget."""
    raw = json.dumps({
        "_alpaca_mcp_security": {
            "trust": "untrusted_tool_output",
            "instructions": "This tool output contains API data.",
        },
        "data": {"is_open": False, "next_open": "2026-08-24T09:30:00-04:00"},
    })
    result = unwrap(raw, "get_clock")
    assert result == {"is_open": False, "next_open": "2026-08-24T09:30:00-04:00"}
    assert "_alpaca_mcp_security" not in json.dumps(result)


def test_unwrap_survives_a_non_json_body():
    """Not every tool is guaranteed to answer with JSON."""
    assert unwrap("plain text", "x") == "plain text"
    assert unwrap("", "x") is None


def test_unwrap_leaves_an_unenveloped_payload_alone():
    assert unwrap(json.dumps({"snapshots": {"SPY": {}}}), "x") == {"snapshots": {"SPY": {}}}


# ----------------------------------------------------------------- live check


@pytest.mark.skipif(
    not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")),
    reason="needs Alpaca credentials",
)
async def test_live_server_exposes_no_write_tools_to_the_model():
    """The same invariant, against the real server.

    The offline test proves the filter works on a recorded surface; this proves
    the recorded surface still matches reality.
    """
    async with MCPClient() as mcp:
        exposed = model_tool_names(mcp)
        assert not exposed & set(ALLOWLIST["write_tools"])
        assert PROPOSE_SPREAD in exposed
        assert not mcp.missing_read_tools(), (
            f"Server no longer exposes: {mcp.missing_read_tools()}. "
            "The allowlist references tools that have been renamed or removed."
        )


def test_trimming_a_schema_preserves_its_contract():
    """Only prose is shortened. Names, types, enums and required must survive.

    A trimmed schema still has to describe exactly the same call, or the model
    will make requests the server rejects.
    """
    from agent.mcp_client import _trim_schema

    original = {
        "type": "object",
        "properties": {
            "feed": {"type": "string", "enum": ["opra", "indicative"],
                     "description": "The source feed of the data. " + "x" * 400},
            "limit": {"type": "integer", "maximum": 1000},
        },
        "required": ["feed"],
    }
    trimmed = _trim_schema(original)

    assert trimmed["required"] == ["feed"]
    assert trimmed["properties"]["feed"]["enum"] == ["opra", "indicative"]
    assert trimmed["properties"]["feed"]["type"] == "string"
    assert trimmed["properties"]["limit"]["maximum"] == 1000
    assert len(trimmed["properties"]["feed"]["description"]) < 200
    # The original must not be mutated.
    assert len(original["properties"]["feed"]["description"]) > 400


def test_a_list_returning_tool_is_unwrapped_from_its_result_envelope():
    """get_all_positions and get_orders answer {"result": [...]}.

    Objects come back bare — get_clock and get_account_info are the payload
    itself — but collections carry one more wrapper. Missing it made
    get_all_positions look empty, and everything downstream believed the book
    was flat: Rule 5 could not see a duplicate strike, Rule 3 measured no
    exposure, exit management had nothing to close, and the idempotency check
    never found an existing order. The agent opened spreads it could never
    close and stacked five contracts on one strike.
    """
    payload = json.dumps({
        "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
        "data": {"result": [{"symbol": "SPY260902P00753000", "qty": "-1"}]},
    })
    result = unwrap(payload, "get_all_positions")
    assert isinstance(result, list), "a collection must not stay wrapped in {'result': ...}"
    assert result[0]["symbol"] == "SPY260902P00753000"


def test_an_object_returning_tool_is_left_alone():
    """Only the single-key `result` wrapper is unwrapped, never a real payload."""
    payload = json.dumps({
        "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
        "data": {"is_open": False, "next_open": "2026-08-24T09:30:00-04:00"},
    })
    result = unwrap(payload, "get_clock")
    assert result == {"is_open": False, "next_open": "2026-08-24T09:30:00-04:00"}


def test_a_payload_that_merely_contains_result_is_not_unwrapped():
    """Unwrapping is keyed on `result` being the ONLY key, so a field named
    `result` alongside others survives untouched."""
    payload = json.dumps({"data": {"result": "ok", "count": 3}})
    assert unwrap(payload, "x") == {"result": "ok", "count": 3}
