"""The boundary between the library and a model provider."""

from typing import TYPE_CHECKING, Protocol

from aox_agent_core.models.types import ProviderRequest, ProviderResponse

if TYPE_CHECKING:
    # Only for annotations: the replay package imports this module.
    from aox_agent_core.replay.keys import PromptKey


class ModelProvider(Protocol):
    """Serves one normalized request.

    Implementations: the Anthropic API, Amazon Bedrock, and the replay and
    recording providers. Errors are raised as ProviderError subclasses with the
    SDK's own error chained as __cause__.

    prompt_key is set for calls made with a PromptRef. Replay and recording
    providers key recordings by it; live providers ignore it.
    """

    async def complete(
        self, request: ProviderRequest, *, prompt_key: "PromptKey | None" = None
    ) -> ProviderResponse: ...


async def close_provider(provider: ModelProvider) -> None:
    """Release a provider's connections on the running loop.

    aclose() is optional: a provider that holds connections defines
    `async def aclose(self) -> None`, and the client calls it when closed.
    """
    aclose = getattr(provider, "aclose", None)
    if aclose is not None:
        await aclose()
