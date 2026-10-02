"""The Amazon Bedrock provider interface. Not implemented in this release."""

from aox_agent_core.errors import ProviderRequestError
from aox_agent_core.models.types import ProviderRequest, ProviderResponse


class BedrockProvider:
    """Calls Claude on Amazon Bedrock through its Messages-API (Mantle) endpoint.

    Bedrock model IDs and prices come from configuration: bedrock.tier_models and
    pricing.bedrock. This class fixes the interface only; the client itself will
    be added in a later release.
    """

    def __init__(self, *, region: str) -> None:
        self._region = region

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise ProviderRequestError(
            "The Bedrock provider is not implemented yet; route this tier to provider "
            "'anthropic' or use replay mode."
        )
