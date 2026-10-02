"""Audit events, stored records, and the chain head used as an external anchor."""

import json
import re
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from aox_agent_core._canonical import canonical_json
from aox_agent_core._model import ActionName, FrozenModel, PrincipalId, Sha256Hex, SubjectId

AUDIT_SCHEMA_VERSION: Final = 1
GENESIS_HASH: Final = "0" * 64
MAX_PAYLOAD_BYTES = 8_192
MAX_SAFE_INTEGER: Final = 2**53 - 1

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
    personal data; record a content hash and a reference instead.

    Numbers must be integers. Floats are rejected because databases reformat
    them, which would change the hashed bytes; write amounts such as costs as
    decimal strings ("0.0123").

    Forbidden keys, floats and oversized payloads fail validation here; the
    audit log re-validates on append and also scans payload strings for secrets.
    """

    action: ActionName
    actor_id: PrincipalId
    subject_id: SubjectId | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_is_small_and_has_no_secret_keys(
        cls, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        forbidden = sorted(_forbidden_keys(payload))
        if forbidden:
            raise ValueError(f"payload has forbidden keys: {', '.join(forbidden)}")

        if _contains_float(payload):
            raise ValueError("payload numbers must be integers; write decimals as strings")
        if _contains_unsafe_integer(payload):
            raise ValueError(
                f"payload integers must be within +/-{MAX_SAFE_INTEGER}, so that any JSON "
                "reader can verify the hash exactly"
            )
        try:
            canonical_json(payload)
        except UnicodeEncodeError as error:
            raise ValueError("payload text is not valid Unicode") from error

        size = len(json.dumps(payload, separators=(",", ":")).encode())
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES}")
        return payload


class UnsealedAuditRecord(FrozenModel):
    """Every field of an audit record except its own hash: the input to the hash.

    seq starts at 1 and has no gaps; prev_hash is the previous record's
    record_hash, or GENESIS_HASH for the first record. Backends store the
    payload as its canonical JSON text, never as a reformatting type such as
    Postgres jsonb, so verify() hashes exactly the bytes append() hashed.
    """

    schema_version: Literal[1] = AUDIT_SCHEMA_VERSION
    seq: Annotated[int, Field(ge=1)]
    event_id: UUID
    occurred_at: AwareDatetime
    action: ActionName
    actor_id: PrincipalId
    subject_id: SubjectId | None
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

    forbidden = {key for key in value if is_secret_shaped_key(key)}
    for nested in value.values():
        forbidden |= _forbidden_keys(nested)
    return forbidden


def is_secret_shaped_key(key: str) -> bool:
    """True when a key, lowercased and stripped to letters and digits, ends in a secret word."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized.endswith(FORBIDDEN_KEY_SUFFIXES)


def _contains_float(value: JsonValue) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, list):
        return any(_contains_float(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    return False


def _contains_unsafe_integer(value: JsonValue) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) > MAX_SAFE_INTEGER
    if isinstance(value, list):
        return any(_contains_unsafe_integer(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_unsafe_integer(item) for item in value.values())
    return False
