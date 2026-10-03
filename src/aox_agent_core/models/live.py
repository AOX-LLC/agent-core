"""Dispatching live calls to the provider each tier names."""

from typing import TYPE_CHECKING

from pydantic import SecretStr

from aox_agent_core.config import AgentCoreConfig, Provider
from aox_agent_core.models.anthropic_provider import AnthropicProvider, refuse_sdk_header_injection
from aox_agent_core.models.bedrock_provider import BedrockProvider
from aox_agent_core.models.provider import ModelProvider, close_provider
from aox_agent_core.models.types import ProviderRequest, ProviderResponse

if TYPE_CHECKING:
    from aox_agent_core.replay.keys import PromptKey


class LiveProviders:
    """Sends each request to the provider it names, creating each provider on first use.

    Construction checks the environment, so a client that cannot make live calls
    fails when it is built rather than on its first call.
    """

    def __init__(self, config: AgentCoreConfig, *, api_key: SecretStr) -> None:
        refuse_sdk_header_injection()
        self._config = config
        self._api_key = api_key
        self._providers: dict[Provider, ModelProvider] = {}

    async def complete(
        self, request: ProviderRequest, *, prompt_key: "PromptKey | None" = None
    ) -> ProviderResponse:
        # Live providers send the request as it is; the prompt key is for recording.
        return await self._provider_for(request.provider).complete(request)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await close_provider(provider)

    def _provider_for(self, provider: Provider) -> ModelProvider:
        if provider not in self._providers:
            self._providers[provider] = self._create(provider)
        return self._providers[provider]

    def _create(self, provider: Provider) -> ModelProvider:
        if provider is Provider.BEDROCK:
            return BedrockProvider(region=self._config.bedrock.region)
        return AnthropicProvider(api_key=self._api_key, settings=self._config.anthropic)
