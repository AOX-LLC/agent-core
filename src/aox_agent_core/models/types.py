"""Provider-neutral request, response and result types.

ProviderRequest and ProviderResponse are what record mode writes to a cassette.
They have no fields for headers, URLs, request IDs or credentials, so none of
those can reach a recording.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Generic, TypeVar

from pydantic import Field, JsonValue

from aox_agent_core._model import FrozenModel
from aox_agent_core.config import Effort, Mode, Provider, Tier

OutputT = TypeVar("OutputT")

TokenCount = Annotated[int, Field(ge=0)]


class Role(StrEnum):
    """Who wrote a message."""

    USER = "user"
    ASSISTANT = "assistant"


class Message(FrozenModel):
    """One conversation turn."""

    role: Role
    content: Annotated[str, Field(min_length=1)]


class Usage(FrozenModel):
    """Token counts reported by the provider for one call."""

    input_tokens: TokenCount
    output_tokens: TokenCount
    cache_creation_input_tokens: TokenCount = 0
    cache_read_input_tokens: TokenCount = 0


class ProviderRequest(FrozenModel):
    """A normalized model request, as sent to a provider and as recorded.

    Optional fields left at None are omitted when the request is hashed for
    replay, so adding a new optional field keeps existing recordings valid.
    """

    provider: Provider
    model: Annotated[str, Field(min_length=1)]
    messages: Annotated[tuple[Message, ...], Field(min_length=1)]
    max_tokens: Annotated[int, Field(gt=0)]
    system: str | None = None
    effort: Effort | None = None
    output_schema: dict[str, JsonValue] | None = None


class ProviderResponse(FrozenModel):
    """A normalized model response, as returned by a provider and as recorded.

    stop_reason is the provider's string ("end_turn", "max_tokens", "refusal" and
    so on). It is kept as a string because providers add new values.
    """

    model: str
    text: str
    stop_reason: str
    usage: Usage


class CallResult(FrozenModel, Generic[OutputT]):
    """What a model call returns: its output plus routing, usage and cost."""

    output: OutputT
    tier: Tier
    task: str | None
    provider: Provider
    model: str
    mode: Mode
    usage: Usage
    cost_usd: Annotated[Decimal, Field(ge=0)]
    latency_ms: Annotated[float, Field(ge=0)]
    stop_reason: str
    trace_id: str | None = None
    attempts: Annotated[int, Field(ge=1)] = 1
