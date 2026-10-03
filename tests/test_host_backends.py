"""A host can bring its own AuditLog and ApprovalQueue wherever agent-core takes one.

agent-core takes an AuditLog in SQLApprovalQueue(audit_log=...). ApprovalQueue
is a protocol for hosts: code typed against it runs on the SQL queue or on a
host's own. mypy checks the in-memory classes below against both protocols.
"""

from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from aox_agent_core import RunContext
from aox_agent_core.approvals import (
    ApprovalQueue,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
    SQLApprovalQueue,
    approval_payload_hash,
)
from aox_agent_core.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditHead,
    AuditLog,
    AuditRecord,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    AuditIntegrityError,
    NotAuthorizedToResolveError,
    NotTheRequesterError,
)
from aox_agent_core.storage import open_database

RUN = RunContext(run_id="run-0001", external_ids={"execution_id": "ex-42"})
REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
PAYLOAD: dict[str, JsonValue] = {"contact_id": "c-1001"}
# The approver side decides which role each action needs.
ROLES = {"crm.update_contact": "ops.approver"}


class InMemoryAuditLog:
    """A host's audit log, chained with agent-core's public hashing."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def append(self, event: AuditEvent) -> AuditRecord:
        head = await self.head()
        unsealed = UnsealedAuditRecord(
            seq=head.seq + 1,
            event_id=uuid4(),
            occurred_at=datetime.now(UTC),
            action=event.action,
            actor_id=event.actor_id,
            subject_id=event.subject_id,
            payload=event.payload,
            run_context=event.context,
            prev_hash=head.record_hash,
        )
        record = AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))
        self.records.append(record)
        return record

    async def iter_records(self, *, after_seq: int = 0) -> AsyncIterator[AuditRecord]:
        for record in self.records[after_seq:]:
            yield record

    async def head(self) -> AuditHead:
        if not self.records:
            return AuditHead(seq=0, record_hash=GENESIS_HASH)
        return AuditHead(seq=self.records[-1].seq, record_hash=self.records[-1].record_hash)

    async def verify(self, *, expected_head: AuditHead | None = None) -> AuditHead:
        previous = GENESIS_HASH
        for record in self.records:
            if record.prev_hash != previous or compute_record_hash(record) != record.record_hash:
                raise AuditIntegrityError(f"Record {record.seq} does not hold.")
            previous = record.record_hash
        head = await self.head()
        if expected_head is not None and expected_head.seq > head.seq:
            raise AuditIntegrityError("Records were removed from the end.")
        return head


class InMemoryApprovalQueue:
    """A host's approval queue: the same rules as the SQL one, in a dict."""

    def __init__(self, audit_log: AuditLog) -> None:
        self._audit_log = audit_log
        self._policy = RoleApproverPolicy(roles_by_action=ROLES)
        self._requests: dict[UUID, ApprovalRequest] = {}

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
    ) -> ApprovalRequest:
        now = datetime.now(UTC)
        request = ApprovalRequest(
            id=uuid4(),
            action=action,
            summary=summary,
            payload_sha256=approval_payload_hash(action, payload),
            requested_by=requested_by.id,
            required_role=required_role,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            run_context=context,
            delegates=frozenset(delegates),
        )
        self._requests[request.id] = request
        await self._audit("approval.requested", requested_by, request, context)
        return request

    async def get(self, request_id: UUID) -> ApprovalRequest:
        if request_id not in self._requests:
            raise ApprovalNotFoundError(f"No approval request {request_id}.")
        return self._requests[request_id]

    async def list_pending(
        self, principal: Principal, *, limit: int = 100, after: UUID | None = None
    ) -> Sequence[ApprovalRequest]:
        now = datetime.now(UTC)
        ordered = list(self._requests.values())
        if after is not None:
            ordered = ordered[[request.id for request in ordered].index(after) + 1 :]
        pending = [
            request
            for request in ordered
            if self._policy.evaluate(principal, request, now=now).allowed
        ]
        return pending[:limit]

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        request = await self.get(request_id)
        if not self._policy.evaluate(principal, request, now=datetime.now(UTC)).allowed:
            raise NotAuthorizedToResolveError(f"Request {request_id} cannot be resolved.")
        status = (
            ApprovalStatus.APPROVED if decision is Decision.APPROVE else ApprovalStatus.REJECTED
        )
        resolved = request.model_copy(
            update={
                "status": status,
                "decision": decision,
                "resolved_by": principal.id,
                "resolved_at": datetime.now(UTC),
                "reason": reason,
            }
        )
        self._requests[request_id] = resolved
        await self._audit("approval.resolved", principal, resolved, context)
        return resolved

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        request = await self.get(request_id)
        if principal.id != request.requested_by and principal.id not in request.delegates:
            raise NotTheRequesterError(f"Request {request_id} is not {principal.id}'s to use.")
        if approval_payload_hash(action, payload) != request.payload_sha256:
            raise ApprovalPayloadMismatchError(f"Request {request_id} approved something else.")
        if request.status is not ApprovalStatus.APPROVED:
            raise ApprovalNotGrantedError(f"Request {request_id} is {request.status.value}.")
        consumed = request.model_copy(
            update={"status": ApprovalStatus.CONSUMED, "consumed_at": datetime.now(UTC)}
        )
        self._requests[request_id] = consumed
        await self._audit("approval.consumed", principal, consumed, context)
        return consumed

    async def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        request = await self.get(request_id)
        if principal.id != request.requested_by:
            raise NotTheRequesterError(f"Request {request_id} is not {principal.id}'s to cancel.")
        if request.status is not ApprovalStatus.PENDING:
            raise ApprovalAlreadyResolvedError(f"Request {request_id} is {request.status.value}.")
        cancelled = request.model_copy(
            update={"status": ApprovalStatus.CANCELLED, "closed_at": datetime.now(UTC)}
        )
        self._requests[request_id] = cancelled
        await self._audit("approval.cancelled", principal, cancelled, context)
        return cancelled

    async def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = 500
    ) -> int:
        moment = now or datetime.now(UTC)
        due = [
            request
            for request in self._requests.values()
            if request.status is ApprovalStatus.PENDING and request.is_expired(moment)
        ]
        for request in due:
            expired = request.model_copy(
                update={"status": ApprovalStatus.EXPIRED, "closed_at": moment}
            )
            self._requests[request.id] = expired
            await self._audit("approval.expired", principal, expired, None)
        return len(due)

    async def _audit(
        self,
        action: str,
        principal: Principal,
        request: ApprovalRequest,
        context: RunContext | None,
    ) -> None:
        await self._audit_log.append(
            AuditEvent(
                action=action,
                actor_id=principal.id,
                subject_id=str(request.id),
                payload={"approval_action": request.action},
                context=context if context is not None else request.run_context,
            )
        )


async def approve_and_act(queue: ApprovalQueue) -> ApprovalRequest:
    """Host code that knows only the protocol."""
    request = await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
        context=RUN,
    )
    assert [pending.id for pending in await queue.list_pending(APPROVER)] == [request.id]
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    return await queue.consume(
        request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
    )


def sql_queue(tmp_path: Path, audit_log: AuditLog) -> ApprovalQueue:
    return SQLApprovalQueue(
        open_database(f"sqlite:///{tmp_path / 'queue.sqlite3'}"),
        audit_log=audit_log,
        policy=RoleApproverPolicy(roles_by_action=ROLES),
    )


@pytest.mark.parametrize("backend", ["sql-queue-host-log", "host-queue-host-log"])
async def test_host_backends_carry_the_whole_flow(tmp_path: Path, backend: str) -> None:
    audit_log = InMemoryAuditLog()
    queue = (
        sql_queue(tmp_path, audit_log)
        if backend == "sql-queue-host-log"
        else InMemoryApprovalQueue(audit_log)
    )

    consumed = await approve_and_act(queue)

    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.run_context == RUN
    assert [(record.action, record.run_context) for record in audit_log.records] == [
        ("approval.requested", RUN),
        ("approval.resolved", RUN),
        ("approval.consumed", RUN),
    ]
    assert (await audit_log.verify()).seq == 3


async def test_the_host_queue_refuses_a_different_payload() -> None:
    queue = InMemoryApprovalQueue(InMemoryAuditLog())
    request = await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    with pytest.raises(ApprovalPayloadMismatchError):
        await queue.consume(
            request.id,
            action="crm.update_contact",
            payload={"contact_id": "c-2"},
            principal=REQUESTER,
        )


async def test_the_sql_queue_audits_denials_to_a_host_log(tmp_path: Path) -> None:
    audit_log = InMemoryAuditLog()
    queue = sql_queue(tmp_path, audit_log)

    with pytest.raises(ApprovalNotFoundError):
        await queue.resolve(uuid4(), decision=Decision.APPROVE, principal=APPROVER, context=RUN)

    (record,) = audit_log.records
    assert (record.action, record.payload, record.run_context) == (
        "approval.resolve_denied",
        {"reason": "not_found"},
        RUN,
    )
