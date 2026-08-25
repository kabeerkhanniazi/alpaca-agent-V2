"""The mediation layer between the model and the Alpaca MCP server.

Everything the model can reach passes through here, and everything it must not
reach is absent rather than merely discouraged.

**The split.** The Alpaca MCP server exposes 74 tools, 17 of which write. The
model is given the eleven read tools in `config/mcp_allowlist.json` plus the
synthetic `propose_spread`, and nothing else. The orchestrator — never the model
— calls the write tools, through a separate method.

**Why this is enforced in code and not by configuration.** `ALPACA_TOOLSETS`
scoping works, but it cannot express this split: the `trading` toolset bundles
`get_all_positions` (needed for portfolio state, the kill switch, and exits)
together with `place_option_order`, `close_all_positions`, and seven other write
tools. No value of that variable yields position reads without order placement.
The toolset filter is still applied as defence in depth; the allowlist is the
enforcement point, because it is the thing under test.

**Two doors, not one.** Filtering the schema list would be enough if the model
could only act through schemas it was given. Belt and braces: :meth:`call_read`
refuses any name outside the allowlist, so even a fabricated tool name is turned
away at the call site rather than forwarded to Alpaca.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .tools import PROPOSE_SPREAD, PROPOSE_SPREAD_SCHEMA

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "mcp_allowlist.json"
DEFAULT_TIMEOUT = 90.0

# The envelope the server wraps every result in. It labels tool output as
# untrusted (the server doing prompt-injection hygiene for us), but it repeats on
# every single result and would burn context in a loop capped at 8 turns, so it
# is stripped before anything reaches the model.
SECURITY_KEY = "_alpaca_mcp_security"


class MCPError(RuntimeError):
    """A tool call failed, or was refused before it was made."""


class ToolNotAllowed(MCPError):
    """A tool outside the read allowlist was requested through the model's path.

    Reaching this means either a bug or an attempt to escape the mediation
    layer. Either way the call does not happen.
    """


def load_allowlist(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class MCPClient:
    """Connects to the Alpaca MCP server and mediates access to it.

    Use as an async context manager::

        async with MCPClient() as mcp:
            tools = mcp.tools_for_model()
            result = await mcp.call_read("get_clock", {})
    """

    def __init__(
        self,
        allowlist: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.config = allowlist or load_allowlist()
        self.read_tools: set[str] = set(self.config["read_tools"])
        # What the model is shown, which is a subset of what the orchestrator may
        # call. Falls back to the full read set when unconfigured.
        self.model_tools: set[str] = set(
            self.config.get("model_tools") or self.config["read_tools"]
        )
        self.write_tools: set[str] = set(self.config["write_tools"])
        self.orchestrator_write_tools: set[str] = set(self.config["orchestrator_write_tools"])
        self.timeout = timeout
        self._env_overrides = env or {}
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._server_tools: dict[str, Any] = {}

        overlap = self.read_tools & self.write_tools
        if overlap:
            raise ValueError(
                f"Tools listed as both read and write: {sorted(overlap)}. "
                "The allowlist is the safety boundary; it cannot be ambiguous."
            )

    # ---------------------------------------------------------- connection

    def server_params(self) -> StdioServerParameters:
        """Build the subprocess spec for `uvx alpaca-mcp-server`.

        PATH and HOME are forwarded explicitly. The child needs them to resolve
        `uvx` and reach its package cache, and a bare cron environment supplies
        neither by default — which is where this would otherwise fail, long
        after it looked fine interactively.
        """
        env = {
            "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
            "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
            "ALPACA_PAPER_TRADE": os.getenv("ALPACA_PAPER_TRADE", "true"),
            "ALPACA_TOOLSETS": self.config["toolsets"],
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
        }
        env.update(self._env_overrides)

        uvx = shutil.which("uvx") or os.path.expanduser("~/.local/bin/uvx")
        if not os.path.exists(uvx):
            raise MCPError(
                "uvx not found. Install uv (https://astral.sh/uv) — the Alpaca MCP "
                "server is launched with `uvx alpaca-mcp-server`."
            )
        return StdioServerParameters(command=uvx, args=["alpaca-mcp-server"], env=env)

    async def __aenter__(self) -> "MCPClient":
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(self.server_params()))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))

        init = await self._session.initialize()
        logger.info("Connected to %s v%s", init.server_info.name, init.server_info.version)

        # `mcp` 2.0.0 exposes snake_case attributes (server_info, input_schema,
        # is_error, structured_content). The camelCase spellings in most online
        # examples raise AttributeError against this version.
        listed = await self._session.list_tools()
        self._server_tools = {tool.name: tool for tool in listed.tools}
        logger.info("Server exposes %d tools under toolsets=%s",
                    len(self._server_tools), self.config["toolsets"])
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self._session = None, None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise MCPError("Not connected. Use `async with MCPClient() as mcp:`.")
        return self._session

    # ------------------------------------------------------------ the split

    def available_read_tools(self) -> list[str]:
        """Allowlisted tools the connected server actually exposes."""
        return sorted(self.read_tools & set(self._server_tools))

    def available_model_tools(self) -> list[str]:
        """The read tools the model is shown — a subset of the allowlist."""
        return sorted(self.model_tools & self.read_tools & set(self._server_tools))

    def missing_read_tools(self) -> list[str]:
        """Allowlisted tools the server did not expose.

        A non-empty result means the server renamed or dropped something — the
        v1-to-v2 rewrite did exactly that — and the affected reads will fail at
        the point of use. Better surfaced at connect time.
        """
        return sorted(self.read_tools - set(self._server_tools))

    def tools_for_model(self) -> list[dict[str, Any]]:
        """The complete tool list handed to the language model.

        Allowlisted read tools, converted to OpenAI function-calling schemas,
        plus the synthetic `propose_spread`. No write tool can appear here: the
        list is built by intersecting the server's tools with the read
        allowlist, so a tool absent from the allowlist cannot be included even
        if the server offers it.
        """
        schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": _first_sentences(self._server_tools[name].description),
                    "parameters": _trim_schema(
                        self._server_tools[name].input_schema
                        or {"type": "object", "properties": {}}
                    ),
                },
            }
            for name in self.available_model_tools()
        ]
        schemas.append(PROPOSE_SPREAD_SCHEMA)
        return schemas

    # -------------------------------------------------------------- calling

    async def call_read(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a read tool on the model's behalf.

        Refuses anything outside the allowlist. This is the second door: the
        model is only ever shown allowlisted schemas, and even so, a name it
        invents or one added to the server later is turned away here rather than
        forwarded to Alpaca.
        """
        if name == PROPOSE_SPREAD:
            raise ToolNotAllowed(
                f"{PROPOSE_SPREAD} is handled by the orchestrator, not dispatched to MCP."
            )
        if name not in self.read_tools:
            raise ToolNotAllowed(
                f"{name!r} is not in the read allowlist. "
                f"Allowed: {sorted(self.read_tools)}"
            )
        return await self._call(name, arguments or {})

    async def call_write(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a write tool. **Orchestrator only — never reachable by the model.**

        Restricted to the two write tools this agent legitimately needs, so a
        future bug cannot turn this into a general-purpose write channel.
        """
        if name not in self.orchestrator_write_tools:
            raise ToolNotAllowed(
                f"{name!r} is not an orchestrator write tool. "
                f"Allowed: {sorted(self.orchestrator_write_tools)}"
            )
        logger.warning("WRITE %s %s", name, arguments)
        return await self._call(name, arguments or {})

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments), timeout=self.timeout
            )
        except asyncio.TimeoutError as exc:
            raise MCPError(f"{name} timed out after {self.timeout}s") from exc

        text = "\n".join(
            block.text for block in result.content
            if getattr(block, "type", None) == "text"
        )

        if result.is_error:
            # Errors arrive as a plain-text block, not JSON — parsing it would
            # raise and hide the actual message.
            raise MCPError(f"{name} failed: {text[:500]}")

        return unwrap(text, name)


# Schemas are resent on every turn, so their size is multiplied by the turn cap.
# Alpaca's descriptions are written for humans browsing docs and run to several
# sentences per parameter; the model needs the first one.
_MAX_DESCRIPTION = 180
_MAX_PARAM_DESCRIPTION = 110


def _first_sentences(text: str | None, limit: int = _MAX_DESCRIPTION) -> str:
    """Keep the leading sentence(s) of a description, up to `limit` characters."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    stop = cut.rfind(". ")
    return (cut[: stop + 1] if stop > limit // 2 else cut).strip()


def _trim_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Shorten parameter descriptions without changing the contract.

    Names, types, enums and `required` are untouched — only prose is shortened,
    so a trimmed schema still describes exactly the same call.
    """
    out = dict(schema)
    properties = out.get("properties")
    if not isinstance(properties, dict):
        return out

    trimmed: dict[str, Any] = {}
    for name, spec in properties.items():
        if isinstance(spec, dict) and isinstance(spec.get("description"), str):
            spec = dict(spec)
            spec["description"] = _first_sentences(spec["description"], _MAX_PARAM_DESCRIPTION)
        trimmed[name] = spec
    out["properties"] = trimmed
    return out


def unwrap(text: str, tool_name: str = "") -> Any:
    """Strip the server's envelope and return the payload.

    Every successful result is a JSON string shaped
    ``{"_alpaca_mcp_security": {...}, "data": {...}}``. The payload is under
    ``data``; the envelope is server-to-client metadata that must not be
    forwarded to the model.
    """
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Not every tool is guaranteed to answer with JSON; hand back the text
        # rather than failing a call that actually succeeded.
        return text

    if isinstance(payload, dict):
        payload.pop(SECURITY_KEY, None)
        if "data" in payload and len(payload) == 1:
            return payload["data"]
    return payload
