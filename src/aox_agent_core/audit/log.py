"""The append-only audit log interface."""

from collections.abc import AsyncIterator
from typing import Protocol

from aox_agent_core.audit.types import AuditEvent, AuditHead, AuditRecord


class AuditLog(Protocol):
    """An append-only, hash-chained log.

    There is no update or delete. Backends also enforce that in the database:
    triggers that abort UPDATE, DELETE and TRUNCATE, and on Postgres a role that
    may only INSERT and SELECT.
    """

    async def append(self, event: AuditEvent) -> AuditRecord:
        """Append one event and return the stored record.

        Raises AuditPayloadRejectedError if a payload string looks like a secret.
        """
        ...

    def iter_records(self, *, after_seq: int = 0) -> AsyncIterator[AuditRecord]: ...

    async def head(self) -> AuditHead:
        """Return the latest seq and hash. Store it somewhere this log's writers cannot reach."""
        ...

    async def verify(self, *, expected_head: AuditHead | None = None) -> AuditHead:
        """Walk the whole chain, check every hash and seq, and return the head.

        The chain alone proves nothing: anyone who can rewrite rows can also rebuild
        every hash after them. Only a head kept somewhere else, passed here as
        expected_head, shows the log has not been rewritten or cut short since that
        head was taken. Raises AuditIntegrityError on any mismatch.
        """
        ...
