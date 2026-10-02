import pytest

from aox_agent_core import Message, Provider, Role
from aox_agent_core.errors import ProviderRequestError
from aox_agent_core.models import BedrockProvider, ProviderRequest


async def test_bedrock_stub_fails_with_a_provider_error() -> None:
    request = ProviderRequest(
        provider=Provider.BEDROCK,
        model="anthropic.claude-sonnet-5-5",
        messages=(Message(role=Role.USER, content="hello"),),
        max_tokens=10,
    )

    with pytest.raises(ProviderRequestError, match="not implemented yet"):
        await BedrockProvider(region="us-east-1").complete(request)
