"""The cassette file format: recorded requests and their responses.

Consuming projects commit cassettes to their own repositories, so this format and
the request hash are stable public API. The field is called request_hash, not
request_key, because secret scanners such as gitleaks flag any high-entropy value
whose name contains "key". format_version changes only with a migration path.
"""

from typing import Annotated, Final, Literal

from pydantic import Field

from aox_agent_core._canonical import sha256_of
from aox_agent_core._model import CassetteName, FrozenModel, Sha256Hex
from aox_agent_core.models.types import ProviderRequest, ProviderResponse

CASSETTE_FORMAT_VERSION: Final = 1


class CassetteEntry(FrozenModel):
    """One recorded call.

    sequence numbers repeated identical requests, so a prompt sent twice in one
    run replays its two responses in order. request_hash is always the hash of
    the request as it was sent, so a redacted entry still matches the live
    request it recorded, but no longer hashes to itself.
    """

    request_hash: Sha256Hex
    sequence: Annotated[int, Field(ge=0)]
    request: ProviderRequest
    response: ProviderResponse


class Cassette(FrozenModel):
    """A named set of recorded calls, stored as one JSON file."""

    format_version: Literal[1] = CASSETTE_FORMAT_VERSION
    name: CassetteName
    entries: tuple[CassetteEntry, ...] = ()


def request_hash(request: ProviderRequest) -> str:
    """Return the SHA-256 hex digest that identifies a request in a cassette.

    The digest covers the request as canonical JSON: keys sorted, no insignificant
    whitespace, UTF-8, with fields that are None left out. Text is hashed as given,
    without Unicode normalization.

    For structured calls the request includes the JSON schema the Anthropic SDK
    generates from the output model, so upgrading anthropic or pydantic can change
    the hash; recordings then miss and must be recorded again.
    """
    return sha256_of(request.model_dump(mode="json", exclude_none=True))
