"""Audit events, stored records, and the chain head used as an external anchor."""

import json
import re
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from aox_agent_core._model import ActionName, FrozenModel, PrincipalId, Sha256Hex

AUDIT_SCHEMA_VERSION: Final = 1
GENESIS_HASH: Final = "0" * 64
MAX_PAYLOAD_BYTES = 8_192

# A payload key is rejected when, lowercased with everything but letters and digits
# removed, it ends with one of these. Suffix matching blocks "client_secret" and
# "x-api-key" but keeps counts such as "input_tokens" usable.
FORBIDDEN_KEY_SUFFIXES: Final = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "token",
)


class AuditEvent(FrozenModel):
    """What a caller appends.

    The payload must be small, JSON-serializable metadata: ids, counts, hashes,
    reason codes. Never credentials, headers, prompt or completion text, or
    personal data; record a content hash and a reference instead. Forbidden keys
    and oversized payloads fail validation here; the audit log additionally scans
    payload strings for secrets when appending.
    """

    action: ActionName
    actor_id: PrincipalId
    subject_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_is_small_and_has_no_secret_keys(
        cls, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        forbidden = sorted(_forbidden_keys(payload))
        if forbidden:
            raise ValueError(f"payload has forbidden keys: {', '.join(forbidden)}")

        size = len(json.dumps(payload, separators=(",", ":")).encode())
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES}")
        return payload


class UnsealedAuditRecord(FrozenModel):
    """Every field of an audit record except its own hash: the input to the hash.

    seq starts at 1 and has no gaps; prev_hash is the previous record's
    record_hash, or GENESIS_HASH for the first record.
    """

    schema_version: Literal[1] = AUDIT_SCHEMA_VERSION
    seq: Annotated[int, Field(ge=1)]
    event_id: UUID
    occurred_at: AwareDatetime
    action: ActionName
    actor_id: PrincipalId
    subject_id: str | None
    payload: dict[str, JsonValue]
    prev_hash: Sha256Hex


class AuditRecord(UnsealedAuditRecord):
    """A stored record, sealed with record_hash = compute_record_hash(its other fields)."""

    record_hash: Sha256Hex


class AuditHead(FrozenModel):
    """The latest record's sequence number and hash. seq 0 is the empty log."""

    seq: Annotated[int, Field(ge=0)]
    record_hash: Sha256Hex


def _forbidden_keys(value: JsonValue) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_forbidden_keys(item) for item in value))
    if not isinstance(value, dict):
        return set()

    forbidden = {key for key in value if _is_forbidden_key(key)}
    for nested in value.values():
        forbidden |= _forbidden_keys(nested)
    return forbidden


def _is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized.endswith(FORBIDDEN_KEY_SUFFIXES)
