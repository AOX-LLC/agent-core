"""The approval queue: submit, resolve, and check before acting."""

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
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
        delegates: Collection[str] = (),
        context: RunContext | None = None,
        include_payload: bool = False,
    ) -> ApprovalRequest:
        """Queue a request that expires after ttl_seconds (at most TTL_SECONDS_MAX).

        The payload's hash is stored. The payload itself is stored only with
        include_payload=True, within a size cap and after the secret scan, and
        every read checks it against the hash, so an approver is shown what the
        hash binds (`ApprovalRequest.payload`). Only the requester may consume the
        approval, unless `delegates` names others.

        Submitting is idempotent. At most one request is open (pending, or approved and
        not yet consumed) for each (requested_by, action, payload hash), and an
        implementation must hold that when calls race, as a unique index does:

        - a repeat with the same required_role, lifetime (ttl_seconds) and delegates
          returns the existing request and records no event;
        - a repeat that matches on those three but differs in any of them raises
          ApprovalConflictError, naming the open request and what differs;
        - `summary`, `context` and `include_payload` are not compared: the first call's stay;
        - an open request already past its lifetime does not count: it is closed as expired
          and the new one is queued.

        An approver may be neither the requester nor one of the delegates.
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

        `principal` must be the requester or one of the request's delegates, or
        NotTheRequesterError is raised. One approval authorizes one run. Raises
        ApprovalPayloadMismatchError if the
        action or payload differ from what was approved, ApprovalNotGrantedError if
        the request is pending or was rejected, ApprovalAlreadyResolvedError if it
        was already consumed or cancelled, and ApprovalExpiredError if it expired.
        """
        ...

    async def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Withdraw a pending request; only its requester may, never a delegate.

        Raises NotTheRequesterError, ApprovalAlreadyResolvedError if it is no
        longer pending, and ApprovalExpiredError if it has expired.
        """
        ...

    async def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = 500
    ) -> int:
        """Store EXPIRED on pending and approved-unused requests past their lifetime and
        return how many.

        Reads must treat such requests as expired whether or not this has run. An
        approval that lapsed unused is EXPIRED and keeps its decision.
        """
        ...
