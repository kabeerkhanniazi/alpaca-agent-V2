"""Provider-agnostic chat-with-tools.

One interface — :func:`chat` — behind three adapters. The orchestrator never
names a provider or a model; both come from config, so switching them is a
config edit rather than a code change, and the journal can record honestly which
model drove each cycle.

    chat(messages, tools) -> ChatResult(text, tool_calls, provider, model, usage)

**Why the shape is normalised here.** OpenRouter and Groq both speak the OpenAI
`/chat/completions` dialect, so they share a code path with different base URLs.
Gemini does not: it wants `functionDeclarations`, returns `functionCall` parts,
and has no notion of a tool-call id. Rather than leak that difference into the
orchestrator, the Gemini adapter synthesises ids and translates both directions.
The orchestrator only ever sees the OpenAI-style shape.

**Failover.** A rate limit or a 5xx on the primary is retried once with backoff,
then the next provider takes over. That event is returned on the result as
``switched_from`` so the caller can journal it — a tonal shift in the rationales
mid-run should be explainable, not mysterious.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 90.0
MAX_ATTEMPTS_PER_PROVIDER = 2
BASE_BACKOFF_SECONDS = 2.0

# Status codes worth waiting out. Everything else in the 4xx range is a settled
# rejection — a bad key or a malformed request does not improve on retry.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}


class LLMError(RuntimeError):
    """A provider call failed.

    ``retryable`` distinguishes "wait and try again" from "this will never work",
    which is what decides between a backoff and an immediate failover.
    """

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    """The normalised result of one model turn."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    # Set when the primary provider failed and a fallback answered instead.
    switched_from: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_journal(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "switched_from": self.switched_from,
            "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in self.tool_calls],
            "text": self.text,
        }


# --------------------------------------------------------------- providers


class Provider:
    """Base adapter. Subclasses translate to and from one vendor's dialect."""

    name = "base"
    env_key = ""
    base_url = ""

    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.getenv(self.env_key, "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self, client: httpx.AsyncClient, messages: list[dict], tools: list[dict] | None
    ) -> ChatResult:
        raise NotImplementedError

    @staticmethod
    def _raise_for_status(response: httpx.Response, provider: str) -> None:
        if response.status_code < 400:
            return
        body = response.text[:400]
        raise LLMError(
            f"{provider} returned {response.status_code}: {body}",
            status=response.status_code,
            retryable=response.status_code in RETRYABLE_STATUS,
        )


class OpenAICompatible(Provider):
    """Adapter for any `/chat/completions` endpoint.

    Covers OpenRouter and Groq. The two differ only in base URL, key, and the
    couple of headers OpenRouter asks for.
    """

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def chat(
        self, client: httpx.AsyncClient, messages: list[dict], tools: list[dict] | None
    ) -> ChatResult:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = await client.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        self._raise_for_status(response, self.name)
        data = response.json()

        # OpenRouter can answer 200 with an error body when an upstream provider
        # fails; treating that as a valid empty turn would silently lose the run.
        if "error" in data and not data.get("choices"):
            err = data["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            raise LLMError(
                f"{self.name} error: {message}",
                status=code if isinstance(code, int) else None,
                retryable=code in RETRYABLE_STATUS if isinstance(code, int) else False,
            )

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(calls)}",
                    name=fn.get("name", ""),
                    arguments=_loads(fn.get("arguments")),
                )
            )

        return ChatResult(
            text=message.get("content") or "",
            tool_calls=calls,
            provider=self.name,
            model=data.get("model") or self.model,
            usage=data.get("usage") or {},
            finish_reason=choice.get("finish_reason") or "",
        )


class OpenRouter(OpenAICompatible):
    name = "openrouter"
    env_key = "OPENROUTER_API_KEY"
    base_url = "https://openrouter.ai/api/v1"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        # OpenRouter attributes traffic with these; they are optional but keep
        # the request identifiable on the account's activity page.
        headers["HTTP-Referer"] = "https://github.com/kabeerkhanniazi/alpaca-agent"
        headers["X-Title"] = "Alpaca MCP Options Agent"
        return headers


class Groq(OpenAICompatible):
    name = "groq"
    env_key = "GROQ_API_KEY"
    base_url = "https://api.groq.com/openai/v1"


class Gemini(Provider):
    """Adapter for Google's generateContent API.

    Gemini's schema differs in three ways that matter: roles are `user`/`model`
    rather than `user`/`assistant`, the system prompt is a separate top-level
    field, and function calls carry no id — so ids are synthesised on the way out
    and matched by name on the way back in.
    """

    name = "gemini"
    env_key = "GEMINI_API_KEY"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: str, api_key: str | None = None):
        super().__init__(model, api_key)
        # Gemini 3.x rejects a follow-up turn whose functionCall parts do not
        # carry back the `thoughtSignature` it issued with them. The signature
        # has no place in the OpenAI-shaped message the orchestrator holds, so
        # it is kept here, keyed by tool-call id, and re-attached on the way out.
        self._signatures: dict[str, str] = {}

    async def chat(
        self, client: httpx.AsyncClient, messages: list[dict], tools: list[dict] | None
    ) -> ChatResult:
        system_text, contents = self._translate_messages(messages)

        payload: dict[str, Any] = {"contents": contents}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            payload["tools"] = [{
                "functionDeclarations": [
                    _strip_schema(t["function"]) for t in tools if t.get("type") == "function"
                ]
            }]

        response = await client.post(
            f"{self.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        self._raise_for_status(response, self.name)
        data = response.json()

        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []

        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            if "text" in part:
                text_chunks.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                call_id = f"gemini_call_{len(calls)}"
                signature = part.get("thoughtSignature")
                if signature:
                    self._signatures[call_id] = signature
                calls.append(
                    ToolCall(
                        id=call_id,
                        name=fc.get("name", ""),
                        arguments=fc.get("args") or {},
                    )
                )

        usage = data.get("usageMetadata") or {}
        return ChatResult(
            text="".join(text_chunks),
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            usage={
                "prompt_tokens": usage.get("promptTokenCount"),
                "completion_tokens": usage.get("candidatesTokenCount"),
                "total_tokens": usage.get("totalTokenCount"),
            },
            finish_reason=candidate.get("finishReason") or "",
        )

    def _translate_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Convert OpenAI-style messages into (system_text, Gemini contents)."""
        system_chunks: list[str] = []
        contents: list[dict] = []

        for msg in messages:
            role = msg.get("role")

            if role == "system":
                system_chunks.append(msg.get("content") or "")

            elif role == "tool":
                # Gemini expects the result keyed by the function's name, which
                # the caller carries on the message as `name`.
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {
                        "name": msg.get("name") or "tool",
                        "response": {"result": msg.get("content")},
                    }}],
                })

            elif role == "assistant":
                parts: list[dict] = []
                if msg.get("content"):
                    parts.append({"text": msg["content"]})
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    part: dict[str, Any] = {"functionCall": {
                        "name": fn.get("name"),
                        "args": _loads(fn.get("arguments")),
                    }}
                    signature = self._signatures.get(call.get("id", ""))
                    if signature:
                        part["thoughtSignature"] = signature
                    parts.append(part)
                if parts:
                    contents.append({"role": "model", "parts": parts})

            else:
                contents.append({"role": "user", "parts": [{"text": msg.get("content") or ""}]})

        return "\n\n".join(c for c in system_chunks if c), contents


PROVIDERS: dict[str, type[Provider]] = {
    "openrouter": OpenRouter,
    "groq": Groq,
    "gemini": Gemini,
}


# ------------------------------------------------------------------ client


class LLMClient:
    """Calls a primary provider, failing over to the configured fallbacks.

    ``chain`` is a list of ``(provider_name, model)`` pairs in priority order.
    Providers whose API key is missing are skipped rather than attempted, so a
    partially-configured environment degrades to whatever is actually available
    instead of failing on the first call.
    """

    def __init__(self, chain: list[tuple[str, str]]):
        if not chain:
            raise ValueError("LLMClient needs at least one (provider, model) pair.")
        self.chain: list[Provider] = []
        for name, model in chain:
            cls = PROVIDERS.get(name)
            if cls is None:
                raise ValueError(f"Unknown provider {name!r}. Known: {sorted(PROVIDERS)}")
            self.chain.append(cls(model))

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMClient":
        """Build from an agent_config dict.

        Expects ``provider``/``model`` and an optional ``fallbacks`` list of
        ``{provider, model}`` entries.
        """
        chain = [(config["provider"], config["model"])]
        for fb in config.get("fallbacks") or []:
            chain.append((fb["provider"], fb["model"]))
        return cls(chain)

    @property
    def available(self) -> list[Provider]:
        return [p for p in self.chain if p.configured]

    def describe(self) -> str:
        return " -> ".join(
            f"{p.name}:{p.model}{'' if p.configured else ' (no key)'}" for p in self.chain
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> ChatResult:
        """Run one turn, failing over on retryable errors.

        Raises :class:`LLMError` only when every configured provider has failed;
        the message names each one, because "the model call failed" without
        saying which provider is not a debuggable log line at 3am.
        """
        providers = self.available
        if not providers:
            raise LLMError(
                "No provider is configured. Set one of: "
                + ", ".join(sorted({p.env_key for p in self.chain}))
            )

        owns_client = client is None
        client = client or httpx.AsyncClient()
        failures: list[str] = []
        primary = providers[0].name

        try:
            for index, provider in enumerate(providers):
                for attempt in range(1, MAX_ATTEMPTS_PER_PROVIDER + 1):
                    try:
                        result = await provider.chat(client, messages, tools)
                        if index > 0:
                            result.switched_from = primary
                            logger.warning(
                                "Failed over from %s to %s:%s", primary, provider.name, provider.model
                            )
                        return result

                    except LLMError as exc:
                        failures.append(f"{provider.name}:{provider.model} -> {exc}")
                        if not exc.retryable or attempt == MAX_ATTEMPTS_PER_PROVIDER:
                            break
                        delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                        delay += random.uniform(0, 0.5)  # de-sync concurrent tickers
                        logger.warning(
                            "%s attempt %d/%d failed (%s) — retrying in %.1fs",
                            provider.name, attempt, MAX_ATTEMPTS_PER_PROVIDER, exc, delay,
                        )
                        await asyncio.sleep(delay)

                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        failures.append(f"{provider.name}:{provider.model} -> {type(exc).__name__}: {exc}")
                        break
        finally:
            if owns_client:
                await client.aclose()

        raise LLMError("All providers failed:\n  " + "\n  ".join(failures))


# ------------------------------------------------------------------ helpers


def _loads(value: Any) -> dict[str, Any]:
    """Parse tool-call arguments, which arrive as a JSON string or a dict.

    A model that emits malformed JSON here is a normal failure mode, not an
    exception worth killing the cycle over — the caller sees empty arguments and
    journals a malformed proposal.
    """
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse tool arguments: %r", value)
        return {}


def _strip_schema(function: dict[str, Any]) -> dict[str, Any]:
    """Render an OpenAI function schema in the subset Gemini accepts.

    Gemini rejects several JSON Schema keywords the OpenAI dialect allows
    (``additionalProperties``, ``$schema``, ``default``), so they are dropped
    rather than passed through and 400'd.
    """
    unsupported = {"additionalProperties", "$schema", "default", "examples", "title"}

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items() if k not in unsupported}
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return {
        "name": function["name"],
        "description": function.get("description", ""),
        "parameters": clean(function.get("parameters") or {"type": "object", "properties": {}}),
    }
