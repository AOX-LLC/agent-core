"""The Anthropic Messages API provider."""

import os
from collections.abc import Mapping

import anthropic
from anthropic.types import JSONOutputFormatParam, MessageParam, OutputConfigParam
from anthropic.types import Message as SdkMessage
from pydantic import SecretStr

from aox_agent_core.config import AnthropicConfig
from aox_agent_core.errors import (
    ConfigError,
    ProviderRequestError,
    ProviderUnavailableError,
    RateLimitedError,
)
from aox_agent_core.models.types import ProviderRequest, ProviderResponse, Usage

# The SDK merges this variable into every request's headers even when the key
# and base URL are passed explicitly, so it could override the library's own
# authentication. It has no way to opt out, so the library refuses to run.
SDK_CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"


def refuse_sdk_header_injection(environ: Mapping[str, str] | None = None) -> None:
    """Raise ConfigError if the environment would inject headers into SDK requests."""
    env = os.environ if environ is None else environ
    if env.get(SDK_CUSTOM_HEADERS_ENV) is not None:
        raise ConfigError(
            f"{SDK_CUSTOM_HEADERS_ENV} is set. The Anthropic SDK would add those headers to "
            "every request, so agent-core will not make live calls until it is unset."
        )


class AnthropicProvider:
    """Calls the Messages API with an explicit key and base URL.

    Passing both explicitly means the SDK never reads ANTHROPIC_API_KEY,
    ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, profiles or workload-identity
    variables. SDK errors are re-raised as ProviderError subclasses with the
    original chained as __cause__.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        settings: AnthropicConfig,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
    ) -> None:
        refuse_sdk_header_injection()
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Send one request and return the normalized response.

        Raises RateLimitedError on HTTP 429, ProviderUnavailableError on a 5xx
        or a connection failure, and ProviderRequestError on any other API error.
        """
        try:
            message = await self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                messages=_messages_param(request),
                system=request.system if request.system is not None else anthropic.omit,
                output_config=_output_config(request) or anthropic.omit,
            )
        except anthropic.APIStatusError as error:
            raise _provider_error(error) from error
        except anthropic.APIConnectionError as error:
            raise ProviderUnavailableError(
                f"Could not reach the Anthropic API: {type(error).__name__}."
            ) from error
        return _normalize(message)

    async def aclose(self) -> None:
        """Close the SDK client and its connections."""
        await self._client.close()


def _messages_param(request: ProviderRequest) -> list[MessageParam]:
    return [
        {"role": message.role.value, "content": message.content} for message in request.messages
    ]


def _output_config(request: ProviderRequest) -> OutputConfigParam:
    output_config: OutputConfigParam = {}
    if request.effort is not None:
        output_config["effort"] = request.effort.value
    if request.output_schema is not None:
        output_format: JSONOutputFormatParam = {
            "type": "json_schema",
            "schema": dict(request.output_schema),
        }
        output_config["format"] = output_format
    return output_config


def _provider_error(error: anthropic.APIStatusError) -> Exception:
    summary = f"Anthropic API returned HTTP {error.status_code} ({error.type})."
    if error.status_code == 429:
        return RateLimitedError(summary)
    if error.status_code >= 500:
        return ProviderUnavailableError(summary)
    return ProviderRequestError(summary)


def _normalize(message: SdkMessage) -> ProviderResponse:
    """Join the text blocks; other block types are dropped, as callers only use text."""
    usage = message.usage
    return ProviderResponse(
        model=message.model,
        text="".join(block.text for block in message.content if block.type == "text"),
        stop_reason=message.stop_reason or "unknown",
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        ),
    )
