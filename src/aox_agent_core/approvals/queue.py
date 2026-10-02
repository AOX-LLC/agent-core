"""The approval queue: submit, resolve, and check before acting."""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from aox_agent_core.approvals.types import ApprovalRequest, Decision, Principal


class ApprovalQueue(Protocol):
    """Stores approval requests and enforces who may resolve them.

    Every submission, resolution and denied attempt writes an audit event; when
    the queue and the audit log share a database, in the same transaction.
    """

    async def submit(
        self,
        *,
        action: str,
        summary: str,
        payload: Mapping[str, JsonValue],
        requested_by: Principal,
        required_role: str,
        ttl_seconds: int,
    ) -> ApprovalRequest:
        """Queue a request that expires after ttl_seconds (at most TTL_SECONDS_MAX).

        The payload's hash is stored; the payload itself is not.
        """
        ...

    async def get(self, request_id: UUID) -> ApprovalRequest: ...

    async def list_pending(self, principal: Principal) -> Sequence[ApprovalRequest]:
        """Pending requests that this principal may resolve."""
        ...

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
    ) -> ApprovalRequest:
        """Approve or reject a pending request, once.

        Raises NotAuthorizedToResolveError if the ApproverPolicy denies it,
        ApprovalAlreadyResolvedError if it is no longer pending, and
        ApprovalExpiredError if it has expired.
        """
        ...

    async def ensure_approved(
        self, request_id: UUID, payload: Mapping[str, JsonValue]
    ) -> ApprovalRequest:
        """Call right before acting. Returns the request if it was approved for exactly
        this payload; raises ApprovalPayloadMismatchError if the payload changed.
        """
        ...
