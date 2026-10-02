"""Recording format 2: one JSON file per recorded exchange.

Consuming projects commit recordings, so this format is stable public API.
Format 1 (agent-core 0.1.0a1, one cassette file per name) is no longer read.
"""

from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from aox_agent_core._model import FrozenModel, Sha256Hex
from aox_agent_core.models.types import ProviderRequest, ProviderResponse
from aox_agent_core.replay.keys import PromptKey

RECORDING_FORMAT_VERSION: Final = 2


class Recording(FrozenModel):
    """One recorded call: its replay key (replay_hash), what produced it, and the response.

    A prompted recording carries its PromptKey and sequence 0. An unprompted
    one has no prompt; its replay_hash is the request hash and its sequence tells
    repeated identical requests apart. The request is stored for review and
    for `cassettes check`; replay never compares it.
    """

    format_version: Literal[2] = RECORDING_FORMAT_VERSION
    # Not called 'key': secret scanners flag high-entropy values under names containing it.
    replay_hash: Sha256Hex
    prompt: PromptKey | None = None
    sequence: Annotated[int, Field(ge=0)] = 0
    request: ProviderRequest
    response: ProviderResponse

    @model_validator(mode="after")
    def _prompted_recordings_have_no_sequence(self) -> Self:
        if self.prompt is not None and self.sequence != 0:
            raise ValueError("a prompted recording is content-addressed and has no sequence")
        return self
