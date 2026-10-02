"""Replay keys: how a call finds its recording.

A call made with a PromptRef is keyed by content, by prompt_key():
    prompt id and version, routed tier, output schema name, normalized inputs,
    attachment SHA-256s and attempt number.
The model ID, run and external ids, timestamps and call order never enter it,
so a recording made on one model replays on another and a re-run reuses the
same recordings. The tier is the one routing chose: a budget drop, which a
price-table change can cause, gives a different key.

A call without a PromptRef is keyed by request_hash(): the whole request as
sent, with repeated identical requests told apart by a sequence number.

Keys and the files they name are stable public API.
"""

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Annotated, Final

from pydantic import Field, JsonValue

from aox_agent_core._canonical import sha256_of
from aox_agent_core._model import FrozenModel, Sha256Hex
from aox_agent_core.config import Tier
from aox_agent_core.models.attachments import Attachment
from aox_agent_core.models.prompts import PromptId, PromptRef
from aox_agent_core.models.types import ProviderRequest

PROMPT_KEY_VERSION: Final = 2


def replay_key(
    prompt: PromptRef,
    *,
    tier: Tier,
    output_schema: str | None,
    inputs: Mapping[str, JsonValue],
    attachments: Sequence[Attachment] = (),
    attempt: int = 1,
) -> str:
    """The SHA-256 key a prompted call is recorded and replayed under.

    output_schema is the output model's class name, or None for text output.
    attempt counts structured-output tries on one tier, from 1.
    """
    return _key_of(
        prompt_id=prompt.id,
        version=prompt.version,
        tier=tier,
        output_schema=output_schema,
        inputs=normalize_inputs(inputs),
        attachment_hashes=tuple(attachment.sha256 for attachment in attachments),
        attempt=attempt,
    )


def request_hash(request: ProviderRequest) -> str:
    """The SHA-256 key of an unprompted call: the request as canonical JSON.

    Keys sorted, no insignificant whitespace, UTF-8, with fields left at their
    defaults (None, no attachments) left out, so adding an optional field keeps
    existing hashes. Attachments enter as their hash, media type and size.
    """
    return sha256_of(request.model_dump(mode="json", exclude_defaults=True))


def normalize_inputs(inputs: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Inputs as the key sees them: every string, keys included, in Unicode NFC form."""
    return {_nfc(name): _normalized(value) for name, value in inputs.items()}


def _normalized(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return normalize_inputs(value)
    return value


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class PromptKey(FrozenModel):
    """What a prompted call is keyed by, plus the hashes that tell a stale recording apart.

    template_sha256, system_sha256 and schema_sha256 are not part of the key: a
    recording whose hashes differ from the call's is stale (StaleRecordingError),
    not missing.
    """

    prompt_id: PromptId
    version: Annotated[int, Field(ge=1)]
    tier: Tier
    output_schema: str | None
    inputs: dict[str, JsonValue]
    attachments: tuple[Attachment, ...] = ()
    attempt: Annotated[int, Field(ge=1)] = 1
    template_sha256: Sha256Hex
    system_sha256: Sha256Hex | None = None
    schema_sha256: Sha256Hex | None = None

    @classmethod
    def for_call(
        cls,
        prompt: PromptRef,
        *,
        tier: Tier,
        output_schema: str | None,
        output_json_schema: dict[str, JsonValue] | None,
        inputs: Mapping[str, JsonValue],
        attachments: Sequence[Attachment],
        attempt: int,
    ) -> "PromptKey":
        return cls(
            prompt_id=prompt.id,
            version=prompt.version,
            tier=tier,
            output_schema=output_schema,
            inputs=normalize_inputs(inputs),
            attachments=tuple(attachment.reference() for attachment in attachments),
            attempt=attempt,
            template_sha256=sha256_of(prompt.template),
            system_sha256=sha256_of(prompt.system) if prompt.system is not None else None,
            schema_sha256=sha256_of(output_json_schema) if output_json_schema is not None else None,
        )

    @property
    def key(self) -> str:
        return _key_of(
            prompt_id=self.prompt_id,
            version=self.version,
            tier=self.tier,
            output_schema=self.output_schema,
            inputs=self.inputs,
            attachment_hashes=tuple(attachment.sha256 for attachment in self.attachments),
            attempt=self.attempt,
        )

    def differs_only_in_tier(self, other: "PromptKey") -> bool:
        """True when `other` is this call on another tier, e.g. before a budget drop."""
        return self.tier != other.tier and self.model_copy(update={"tier": other.tier}).key == (
            other.key
        )

    def stale_parts(self, recorded: "PromptKey") -> list[str]:
        """Which of template, system prompt and output schema changed since `recorded`."""
        changes = {
            "template": self.template_sha256 != recorded.template_sha256,
            "system prompt": self.system_sha256 != recorded.system_sha256,
            "output schema": self.schema_sha256 != recorded.schema_sha256,
        }
        return [part for part, changed in changes.items() if changed]


def _key_of(
    *,
    prompt_id: str,
    version: int,
    tier: Tier,
    output_schema: str | None,
    inputs: dict[str, JsonValue],
    attachment_hashes: tuple[str, ...],
    attempt: int,
) -> str:
    return sha256_of(
        {
            "key_version": PROMPT_KEY_VERSION,
            "prompt_id": prompt_id,
            "version": version,
            "tier": tier.value,
            "output_schema": output_schema,
            "inputs": inputs,
            "attachments": list(attachment_hashes),
            "attempt": attempt,
        }
    )
