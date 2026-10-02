"""Hashing records into a chain."""

from aox_agent_core.audit.types import UnsealedAuditRecord


def compute_record_hash(record: UnsealedAuditRecord) -> str:
    """Return the SHA-256 hex digest of a record's fields, prev_hash included.

    Only UnsealedAuditRecord's fields are hashed, so an AuditRecord can be passed
    to check its stored record_hash. The input is canonical JSON: keys sorted, no
    insignificant whitespace, UTF-8, timestamps as UTC ISO 8601 with microseconds.
    Changing any of that breaks verification of existing logs, so it changes only
    with a new schema_version.
    """
    raise NotImplementedError("compute_record_hash is not implemented yet.")
