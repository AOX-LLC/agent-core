"""The boundary between the library and a model provider."""

from typing import Protocol

from aox_agent_core.models.types import ProviderRequest, ProviderResponse


class ModelProvider(Protocol):
    """Serves one normalized request.

    Implementations: the Anthropic API, Amazon Bedrock, and the replay and
    recording providers. Errors are raised as ProviderError subclasses with the
    SDK's own error chained as __cause__.
    """

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


async def close_provider(provider: ModelProvider) -> None:
    """Release a provider's connections on the running loop.

    aclose() is optional: a provider that holds connections defines
    `async def aclose(self) -> None`, and the client calls it when closed.
    """
    aclose = getattr(provider, "aclose", None)
    if aclose is not None:
        await aclose()
