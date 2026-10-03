"""The approval queue: submit, resolve, and check before acting."""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from aox_agent_core.approvals.types import ApprovalRequest, Decision, Principal
from aox_agent_core.context import RunContext


class ApprovalQueue(Protocol):
    """Stores approval requests, enforces who may resolve them, and allows one run each.

    Every submission, resolution and denied attempt writes an audit event; when
    the queue and the audit log share a database, in the same transaction.

    `context` names the run making each call. submit() stores it on the request;
    resolve() and consume() put theirs on their audit events, or the request's
    own when they are given none.
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
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Queue a request that expires after ttl_seconds (at most TTL_SECONDS_MAX).

        The payload's hash is stored; the payload itself is not.
        """
        ...

    async def get(self, request_id: UUID) -> ApprovalRequest: ...

    async def list_pending(
        self, principal: Principal, *, limit: int = 100, after: UUID | None = None
    ) -> Sequence[ApprovalRequest]:
        """Up to `limit` pending, unexpired requests this principal may resolve, oldest first.

        Pass the last request's id as `after` to read the next page.
        """
        ...

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Approve or reject a pending request, once.

        Raises NotAuthorizedToResolveError if the ApproverPolicy denies it,
        ApprovalAlreadyResolvedError if it is no longer pending, and
        ApprovalExpiredError if it has expired.
        """
        ...

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Call right before acting, as `principal`, the one about to act.

        Atomically moves an approved request to CONSUMED.

        One approval authorizes one run. Raises ApprovalPayloadMismatchError if the
        action or payload differ from what was approved, ApprovalNotGrantedError if
        the request is pending or was rejected, ApprovalAlreadyResolvedError if it
        was already consumed or cancelled, and ApprovalExpiredError if it expired.
        """
        ...
