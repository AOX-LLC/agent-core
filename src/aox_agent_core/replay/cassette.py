"""The cassette file format: recorded requests and their responses.

Consuming projects commit cassettes to their own repositories, so this format and
the request key are stable public API. format_version changes only with a
migration path.
"""

from typing import Annotated, Final, Literal

from pydantic import Field

from aox_agent_core._model import CassetteName, FrozenModel, Sha256Hex
from aox_agent_core.models.types import ProviderRequest, ProviderResponse

CASSETTE_FORMAT_VERSION: Final = 1


class CassetteEntry(FrozenModel):
    """One recorded call.

    sequence numbers repeated identical requests, so a prompt sent twice in one
    run replays its two responses in order.
    """

    request_key: Sha256Hex
    sequence: Annotated[int, Field(ge=0)]
    request: ProviderRequest
    response: ProviderResponse


class Cassette(FrozenModel):
    """A named set of recorded calls, stored as one JSON file."""

    format_version: Literal[1] = CASSETTE_FORMAT_VERSION
    name: CassetteName
    entries: tuple[CassetteEntry, ...] = ()


def request_key(request: ProviderRequest) -> str:
    """Return the SHA-256 hex digest that identifies a request in a cassette.

    The digest covers the request as canonical JSON: keys sorted, no insignificant
    whitespace, UTF-8, with fields that are None left out.
    """
    raise NotImplementedError("request_key is not implemented yet.")
