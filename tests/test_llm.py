"""The provider layer: response parsing, dialect translation, and failover.

No test here touches the network. The Stage 2 gate (`spikes/gate_stage2.py`)
covers the live round-trip; these cover the translation logic that would
otherwise only break against a real provider, which is a slow and expensive
place to discover a bug.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.llm import (
    ChatResult,
    Gemini,
    Groq,
    LLMClient,
    LLMError,
    OpenRouter,
    _loads,
    _strip_schema,
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_option_quote",
        "description": "Quote one contract.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"symbol": {"type": "string", "default": "SPY"}},
            "required": ["symbol"],
        },
    },
}]


def transport(handler):
    """Wrap a request handler as an httpx client that never leaves the process."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------- OpenAI-compatible parsing


def openai_response(*, content=None, tool_calls=None, model="test-model"):
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_parses_a_tool_call():
    def handler(request):
        return httpx.Response(200, json=openai_response(tool_calls=[{
            "id": "call_1",
            "function": {"name": "get_option_quote",
                         "arguments": '{"symbol": "SPY260831P00752000"}'},
        }]))

    async with transport(handler) as client:
        result = await OpenRouter("m", api_key="k").chat(client, [], TOOLS)

    assert result.wants_tools
    call = result.tool_calls[0]
    assert call.name == "get_option_quote"
    assert call.arguments == {"symbol": "SPY260831P00752000"}
    assert result.usage["total_tokens"] == 15


@pytest.mark.asyncio
async def test_parses_a_plain_text_answer():
    def handler(request):
        return httpx.Response(200, json=openai_response(content="The bid is 1.56."))

    async with transport(handler) as client:
        result = await Groq("m", api_key="k").chat(client, [], None)

    assert not result.wants_tools
    assert result.text == "The bid is 1.56."
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_tools_are_omitted_when_none_are_given():
    """A bare chat must not send an empty tools array — some providers 400 on it."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=openai_response(content="hi"))

    async with transport(handler) as client:
        await Groq("m", api_key="k").chat(client, [{"role": "user", "content": "hi"}], None)

    assert "tools" not in seen
    assert "tool_choice" not in seen


@pytest.mark.asyncio
async def test_a_200_with_an_error_body_is_still_an_error():
    """OpenRouter can answer 200 with an error when an upstream provider fails.

    Treating that as a valid empty turn would make the model look like it
    declined to act, which is a completely different journal entry.
    """
    def handler(request):
        return httpx.Response(200, json={"error": {"message": "upstream is down", "code": 502}})

    async with transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await OpenRouter("m", api_key="k").chat(client, [], None)

    assert "upstream is down" in str(exc.value)
    assert exc.value.retryable


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (400, False), (401, False), (404, False)],
)
@pytest.mark.asyncio
async def test_retryable_classification(status, retryable):
    def handler(request):
        return httpx.Response(status, text="nope")

    async with transport(handler) as client:
        with pytest.raises(LLMError) as exc:
            await Groq("m", api_key="k").chat(client, [], None)

    assert exc.value.retryable is retryable


# ----------------------------------------------------------- Gemini dialect


def test_gemini_splits_out_the_system_prompt():
    system, contents = Gemini('gemini-x', api_key='k')._translate_messages([
        {"role": "system", "content": "You are an analyst."},
        {"role": "user", "content": "What is the bid?"},
    ])
    assert system == "You are an analyst."
    assert contents == [{"role": "user", "parts": [{"text": "What is the bid?"}]}]


def test_gemini_renames_assistant_to_model():
    _, contents = Gemini('gemini-x', api_key='k')._translate_messages([{"role": "assistant", "content": "Working on it."}])
    assert contents[0]["role"] == "model"


def test_gemini_translates_a_tool_call_and_its_result():
    _, contents = Gemini('gemini-x', api_key='k')._translate_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "get_option_quote",
                                      "arguments": '{"symbol": "SPY"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "get_option_quote",
         "content": '{"bid": 1.56}'},
    ])

    assert contents[0]["parts"][0]["functionCall"] == {
        "name": "get_option_quote", "args": {"symbol": "SPY"}
    }
    # Gemini has no tool role; results come back as a user functionResponse.
    assert contents[1]["role"] == "user"
    assert contents[1]["parts"][0]["functionResponse"]["name"] == "get_option_quote"


def test_gemini_schema_drops_keywords_it_rejects():
    cleaned = _strip_schema(TOOLS[0]["function"])
    assert "additionalProperties" not in cleaned["parameters"]
    assert "default" not in cleaned["parameters"]["properties"]["symbol"]
    # The parts Gemini does need must survive the strip.
    assert cleaned["name"] == "get_option_quote"
    assert cleaned["parameters"]["required"] == ["symbol"]


@pytest.mark.asyncio
async def test_gemini_parses_a_function_call():
    def handler(request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [
                {"functionCall": {"name": "get_option_quote", "args": {"symbol": "SPY"}}}
            ]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4,
                              "totalTokenCount": 12},
        })

    async with transport(handler) as client:
        result = await Gemini("gemini-x", api_key="k").chat(client, [], TOOLS)

    assert result.tool_calls[0].name == "get_option_quote"
    assert result.tool_calls[0].arguments == {"symbol": "SPY"}
    assert result.usage["total_tokens"] == 12


# ------------------------------------------------------------------ failover


@pytest.mark.asyncio
async def test_failover_moves_to_the_next_provider_and_records_it():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "openrouter" in str(request.url):
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=openai_response(content="fallback answered"))

    client = LLMClient([("openrouter", "a"), ("groq", "b")])
    for provider in client.chain:
        provider.api_key = "k"

    async with transport(handler) as http:
        result = await client.chat([{"role": "user", "content": "hi"}], None, client=http)

    assert result.text == "fallback answered"
    assert result.provider == "groq"
    # The switch must be visible: it explains any tonal shift in the rationales.
    assert result.switched_from == "openrouter"


@pytest.mark.asyncio
async def test_a_non_retryable_error_fails_over_without_retrying():
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        if "openrouter" in str(request.url):
            return httpx.Response(401, text="bad key")
        return httpx.Response(200, json=openai_response(content="ok"))

    client = LLMClient([("openrouter", "a"), ("groq", "b")])
    for provider in client.chain:
        provider.api_key = "k"

    async with transport(handler) as http:
        await client.chat([{"role": "user", "content": "hi"}], None, client=http)

    # One attempt at the bad key, not two: a 401 never improves on retry.
    assert sum("openrouter" in url for url in attempts) == 1


@pytest.mark.asyncio
async def test_providers_without_a_key_are_skipped_not_attempted():
    def handler(request):
        assert "groq" in str(request.url), f"called an unconfigured provider: {request.url}"
        return httpx.Response(200, json=openai_response(content="ok"))

    client = LLMClient([("openrouter", "a"), ("groq", "b")])
    client.chain[0].api_key = ""
    client.chain[1].api_key = "k"

    async with transport(handler) as http:
        result = await client.chat([{"role": "user", "content": "hi"}], None, client=http)

    assert result.provider == "groq"
    # It answered first, so it is not a failover.
    assert result.switched_from is None


@pytest.mark.asyncio
async def test_every_provider_failing_names_all_of_them():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = LLMClient([("openrouter", "a"), ("groq", "b")])
    for provider in client.chain:
        provider.api_key = "k"

    async with transport(handler) as http:
        with pytest.raises(LLMError) as exc:
            await client.chat([{"role": "user", "content": "hi"}], None, client=http)

    message = str(exc.value)
    assert "openrouter:a" in message and "groq:b" in message


@pytest.mark.asyncio
async def test_no_configured_provider_is_a_clear_error():
    client = LLMClient([("openrouter", "a")])
    client.chain[0].api_key = ""

    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        await client.chat([{"role": "user", "content": "hi"}], None)


def test_client_builds_from_config_in_priority_order():
    client = LLMClient.from_config({
        "provider": "openrouter", "model": "primary",
        "fallbacks": [{"provider": "groq", "model": "second"},
                      {"provider": "gemini", "model": "third"}],
    })
    assert [(p.name, p.model) for p in client.chain] == [
        ("openrouter", "primary"), ("groq", "second"), ("gemini", "third")
    ]


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"symbol": "SPY"}', {"symbol": "SPY"}),
        ({"symbol": "SPY"}, {"symbol": "SPY"}),
        ("", {}),
        (None, {}),
        ("not json at all", {}),
        ("[1, 2]", {"value": [1, 2]}),
    ],
)
def test_tool_argument_parsing_never_raises(raw, expected):
    """Malformed arguments are a normal model failure, not a crash.

    The orchestrator journals these as a malformed proposal and moves on, which
    it cannot do if parsing raises out of the provider layer.
    """
    assert _loads(raw) == expected


def test_result_journals_the_provider_and_model():
    result = ChatResult(text="hi", provider="groq", model="gpt-oss-120b",
                        usage={"total_tokens": 5}, finish_reason="stop")
    entry = result.to_journal()
    assert entry["provider"] == "groq"
    assert entry["model"] == "gpt-oss-120b"
