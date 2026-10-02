"""Hashing records into a chain."""

from datetime import UTC, datetime

from aox_agent_core._canonical import sha256_of
from aox_agent_core.audit.types import UnsealedAuditRecord


def canonical_timestamp(moment: datetime) -> str:
    """UTC ISO 8601 with exactly six fractional digits, as hashed and stored.

    A naive datetime is refused: converting it would silently assume local time.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"timestamp {moment!r} has no time zone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_record_hash(record: UnsealedAuditRecord) -> str:
    """Return the SHA-256 hex digest of a record's fields, prev_hash included.

    Only UnsealedAuditRecord's fields are hashed, so an AuditRecord can be passed
    to check its stored record_hash. The input is canonical JSON: keys sorted, no
    insignificant whitespace, UTF-8, timestamps as UTC ISO 8601 with microseconds.
    Changing any of that breaks verification of existing logs, so it changes only
    with a new schema_version.
    """
    return sha256_of(
        {
            "schema_version": record.schema_version,
            "seq": record.seq,
            "event_id": str(record.event_id),
            "occurred_at": canonical_timestamp(record.occurred_at),
            "action": record.action,
            "actor_id": record.actor_id,
            "subject_id": record.subject_id,
            "payload": record.payload,
            "prev_hash": record.prev_hash,
        }
    )
