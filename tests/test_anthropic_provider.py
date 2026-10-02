"""The Anthropic provider against recorded and hand-written API responses (see fixtures/sdk)."""

import json
from typing import Literal

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, SecretStr

from aox_agent_core import AgentClient, Effort, Message, Provider, Role, Tier
from aox_agent_core.errors import ProviderRequestError, ProviderUnavailableError, RateLimitedError
from aox_agent_core.models import AnthropicProvider, ProviderRequest
from support import FIXTURES, make_config


class Triage(BaseModel):
    queue: Literal["billing", "technical", "account"]
    urgent: bool
    summary: str


def fixture(name: str) -> object:
    return json.loads((FIXTURES / "sdk" / f"{name}.json").read_text())


def provider_returning(
    status: int, body: object, seen: list[httpx2.Request] | None = None
) -> AnthropicProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body)

    settings = make_config(anthropic={"max_retries": 0}).anthropic
    return AnthropicProvider(
        api_key=SecretStr("test-key-not-real"),
        settings=settings,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def request(**fields: object) -> ProviderRequest:
    return ProviderRequest.model_validate(
        {
            "provider": Provider.ANTHROPIC,
            "model": "claude-sonnet-5-5",
            "messages": (Message(role=Role.USER, content="hello"),),
            "max_tokens": 200,
            **fields,
        }
    )


async def test_text_reply_skips_thinking_blocks_and_keeps_cache_usage() -> None:
    reply = await provider_returning(200, fixture("text_reply_with_thinking")).complete(request())

    body = fixture("text_reply_with_thinking")
    assert isinstance(body, dict)
    text_blocks = [block["text"] for block in body["content"] if block["type"] == "text"]
    assert reply.text == "".join(text_blocks)
    assert reply.stop_reason == "end_turn"
    assert reply.usage.cache_creation_input_tokens == body["usage"]["cache_creation_input_tokens"]
    assert reply.usage.cache_read_input_tokens == body["usage"]["cache_read_input_tokens"]


async def test_refusal_is_returned_for_the_client_to_handle() -> None:
    reply = await provider_returning(200, fixture("refusal")).complete(request())

    assert (reply.stop_reason, reply.text) == ("refusal", "")


async def test_request_body_carries_system_effort_and_schema() -> None:
    seen: list[httpx2.Request] = []
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    provider = provider_returning(200, fixture("triage_valid"), seen)

    await provider.complete(request(system="be brief", effort=Effort.LOW, output_schema=schema))
    await provider.complete(request())

    with_options, plain = (json.loads(sent.content) for sent in seen)
    assert with_options["system"] == "be brief"
    assert with_options["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": schema},
    }
    assert "system" not in plain
    assert "output_config" not in plain


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (429, "error_rate_limit", RateLimitedError),
        (400, "error_invalid_request", ProviderRequestError),
        (529, "error_overloaded", ProviderUnavailableError),
    ],
)
async def test_http_errors_become_provider_errors(
    status: int, body: str, error: type[Exception]
) -> None:
    provider = provider_returning(status, fixture(body))

    with pytest.raises(error, match=f"HTTP {status}") as caught:
        await provider.complete(request())

    assert isinstance(caught.value.__cause__, anthropic.APIStatusError)
    assert "test-key-not-real" not in str(caught.value)


async def test_connection_failure_is_unavailable() -> None:
    def refuse(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    provider = AnthropicProvider(
        api_key=SecretStr("test-key-not-real"),
        settings=make_config(anthropic={"max_retries": 0}).anthropic,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(refuse)),
    )

    with pytest.raises(ProviderUnavailableError, match="Could not reach"):
        await provider.complete(request())


async def test_max_tokens_stop_is_reported() -> None:
    reply = await provider_returning(200, fixture("max_tokens")).complete(request())

    assert reply.stop_reason == "max_tokens"


async def test_structured_retry_end_to_end_through_the_real_provider() -> None:
    bodies = [fixture("triage_invalid"), fixture("triage_valid")]

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=bodies.pop(0))

    provider = AnthropicProvider(
        api_key=SecretStr("test-key-not-real"),
        settings=make_config().anthropic,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    client = AgentClient(make_config(), provider=provider)

    result = await client.call("triage this", output=Triage, tier=Tier.SMALL)

    assert result.output.queue == "billing"
    assert result.attempts == 2
    assert bodies == []


async def test_live_recorded_reply_parses_with_its_real_model_string() -> None:
    reply = await provider_returning(200, fixture("text_reply")).complete(request())

    assert reply.model == "claude-haiku-4-5-20251001"
    assert reply.stop_reason == "end_turn"
    assert reply.text
