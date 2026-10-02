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
