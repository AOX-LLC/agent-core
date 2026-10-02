"""RunContext: which run a call, an approval or an audit record belongs to."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Final, Self

from pydantic import (
    Field,
    StringConstraints,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from aox_agent_core._canonical import canonical_json
from aox_agent_core._model import FrozenModel

MAX_EXTERNAL_IDS: Final = 16
MAX_CONTEXT_BYTES: Final = 2_048

# Validation context for a run context read back from storage: it was scanned for
# secrets when written, and is not scanned again, so a secret pattern added later
# cannot make an existing audit chain unreadable.
STORED_CONTEXT: Final = "aox_agent_core.stored_run_context"


# Opaque identifiers only: no '@', so an email address cannot pass as an id.
OpaqueId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
ExternalIdName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class RunContext(FrozenModel):
    """The run behind a call, plus the host's own ids for it.

        RunContext(run_id=str(run.id), external_ids={"n8n_execution_id": "4812"})

    It flows into span attributes, audit records and approvals, never into
    replay keys. Values must be opaque ids: at most MAX_EXTERNAL_IDS of them, at
    most MAX_CONTEXT_BYTES in all, and nothing that looks like a secret, by
    name or by value (the default patterns here; the audit log and the client
    also apply their own configured patterns). external_ids is read-only.
    """

    run_id: OpaqueId
    external_ids: Mapping[ExternalIdName, OpaqueId] = Field(
        default_factory=lambda: MappingProxyType({})
    )

    @field_validator("external_ids", mode="after")
    @classmethod
    def _read_only(cls, external_ids: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(sorted(external_ids.items())))

    @field_serializer("external_ids")
    def _as_dict(self, external_ids: Mapping[str, str]) -> dict[str, str]:
        return dict(external_ids)

    def __hash__(self) -> int:
        return hash((self.run_id, tuple(self.external_ids.items())))

    @model_validator(mode="after")
    def _small_and_not_secret(self, info: ValidationInfo) -> Self:
        # Imported here: the audit and replay modules import this one.
        from aox_agent_core.audit.types import is_secret_shaped_key
        from aox_agent_core.replay.scrub import PatternScrubber

        if len(self.external_ids) > MAX_EXTERNAL_IDS:
            raise ValueError(f"at most {MAX_EXTERNAL_IDS} external ids")
        size = len(canonical_json(self.as_json()))
        if size > MAX_CONTEXT_BYTES:
            raise ValueError(f"run context is {size} bytes; the limit is {MAX_CONTEXT_BYTES}")
        secret_names = sorted(name for name in self.external_ids if is_secret_shaped_key(name))
        if secret_names:
            raise ValueError(f"external id names look like secrets: {', '.join(secret_names)}")
        if info.context and info.context.get(STORED_CONTEXT):
            return self
        if PatternScrubber().find_secrets(self.as_json()):
            raise ValueError("a run context value looks like a secret")
        return self

    def as_json(self) -> dict[str, Any]:
        """The context as plain JSON, as audit records store and hash it."""
        return {"run_id": self.run_id, "external_ids": dict(sorted(self.external_ids.items()))}
