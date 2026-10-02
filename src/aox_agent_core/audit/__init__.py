"""An append-only, hash-chained audit log (SQLite by default, Postgres optional)."""

from aox_agent_core.audit.chain import compute_record_hash
from aox_agent_core.audit.log import AuditLog
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.audit.types import (
    AUDIT_SCHEMA_VERSION,
    FORBIDDEN_KEY_SUFFIXES,
    GENESIS_HASH,
    MAX_PAYLOAD_BYTES,
    AuditEvent,
    AuditHead,
    AuditRecord,
    UnsealedAuditRecord,
)

__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "FORBIDDEN_KEY_SUFFIXES",
    "GENESIS_HASH",
    "MAX_PAYLOAD_BYTES",
    "AuditEvent",
    "AuditHead",
    "AuditLog",
    "AuditRecord",
    "SQLAuditLog",
    "UnsealedAuditRecord",
    "compute_record_hash",
]
