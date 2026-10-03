"""Cancelling, expiring and reading approval requests, on SQLite and Postgres."""

import asyncio
import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aox_agent_core import RunContext
from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
)
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ConfigError,
    NotTheRequesterError,
)
from databases import ControlDatabase, SplitQueue, split_queue

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
DELEGATE = Principal(id="svc-crm-writer", kind=PrincipalKind.SERVICE)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
SWEEPER = Principal(id="svc-sweeper", kind=PrincipalKind.SERVICE)
RUN = RunContext(run_id="run-0001")
NOW = datetime.now(UTC).replace(microsecond=0)


class Clock:
    def __init__(self) -> None:
        # Fresh for each test: the Postgres guard bounds a decision's time by its own clock.
        self.start = datetime.now(UTC).replace(microsecond=0)
        self.now = self.start

    def __call__(self) -> datetime:
        return self.now


_submitted = itertools.count(1001)


async def submit(queue: SplitQueue, **options: Any) -> ApprovalRequest:
    """A request with a payload of its own: only one request may be open for the same one."""
    return await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact",
        payload=options.pop("payload", {"contact_id": f"c-{next(_submitted)}"}),
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
        **options,
    )


async def audit_trail(database: ControlDatabase) -> list[tuple[str, str, Any, Any]]:
    return [
        (record.action, record.actor_id, record.payload.get("reason"), record.run_context)
        async for record in SQLAuditLog(database.database).iter_records()
    ]


async def expired_requests(queue: SplitQueue, clock: Clock, count: int) -> list[ApprovalRequest]:
    """Requests submitted two hours ago with a one-hour lifetime: over by any clock."""
    clock.now = clock.start - timedelta(hours=2)
    requests = [await submit(queue, context=RUN) for _ in range(count)]
    clock.now = clock.start
    return requests


# Cancelling


async def test_the_requester_cancels_a_pending_request(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)
    request = await submit(queue, context=RUN)

    cancelled = await queue.cancel(request.id, principal=REQUESTER, reason="duplicate")

    assert cancelled.status is ApprovalStatus.CANCELLED
    assert cancelled.closed_at is not None
    assert await queue.get(request.id) == cancelled
    with pytest.raises(ApprovalAlreadyResolvedError):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    records = [r async for r in SQLAuditLog(control_database.database).iter_records()]
    assert (records[1].action, records[1].payload) == (
        "approval.cancelled",
        {"approval_action": "crm.update_contact", "cancel_reason": "duplicate"},
    )
    assert records[1].run_context == RUN


@pytest.mark.parametrize(
    ("principal", "error", "reason"),
    [
        (DELEGATE, NotTheRequesterError, "not_requester"),
        (APPROVER, NotTheRequesterError, "not_requester"),
    ],
    ids=["delegate", "approver-principal"],
)
async def test_only_the_requester_may_cancel(
    control_database: ControlDatabase, principal: Principal, error: type[Exception], reason: str
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue, delegates={DELEGATE.id})

    with pytest.raises(error):
        await queue.cancel(request.id, principal=principal)

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING
    assert (await audit_trail(control_database))[-1][:3] == (
        "approval.cancel_denied",
        principal.id,
        reason,
    )


async def test_a_decided_or_expired_request_cannot_be_cancelled(
    control_database: ControlDatabase,
) -> None:
    clock = Clock()
    queue = split_queue(control_database, clock=clock)
    approved = await submit(queue)
    await queue.resolve(approved.id, decision=Decision.APPROVE, principal=APPROVER)
    (expired,) = await expired_requests(queue, clock, 1)

    with pytest.raises(ApprovalAlreadyResolvedError):
        await queue.cancel(approved.id, principal=REQUESTER)
    with pytest.raises(ApprovalExpiredError):
        await queue.cancel(expired.id, principal=REQUESTER)

    reasons = [reason for action, _, reason, _ in await audit_trail(control_database)]
    assert reasons[-2:] == ["not_pending", "expired"]


async def test_the_approver_side_cannot_cancel(control_database: ControlDatabase) -> None:
    if control_database.backend == "sqlite":
        pytest.skip("SQLite has no sides")
    queue = split_queue(control_database)
    request = await submit(queue)

    with pytest.raises(ConfigError, match="cannot cancel"):
        await queue.approver.cancel(request.id, principal=REQUESTER)


# Expiring


async def test_reads_report_expiry_before_any_sweep(control_database: ControlDatabase) -> None:
    clock = Clock()
    queue = split_queue(control_database, clock=clock)
    (request,) = await expired_requests(queue, clock, 1)

    read = await queue.get(request.id)

    assert (read.status, read.closed_at) == (ApprovalStatus.EXPIRED, request.expires_at)
    assert control_database.raw(
        f"SELECT status FROM agent_core_approvals WHERE id = '{request.id}'"
    ) == [("pending",)]
    assert await queue.list_pending(APPROVER) == []
    with pytest.raises(ApprovalExpiredError):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)


@pytest.mark.parametrize("side", ["requester", "approver"])
async def test_the_sweep_stores_expiry_and_audits_each_request(
    control_database: ControlDatabase, side: str
) -> None:
    clock = Clock()
    queue = split_queue(control_database, clock=clock)
    due = await expired_requests(queue, clock, 3)
    live = await submit(queue)
    sweeping = queue.requester if side == "requester" else queue.approver

    assert await sweeping.expire_due(principal=SWEEPER, limit=2) == 3
    assert await sweeping.expire_due(principal=SWEEPER) == 0

    for request in due:
        stored = await queue.get(request.id)
        assert stored.status is ApprovalStatus.EXPIRED
        assert stored.closed_at is not None
    assert (await queue.get(live.id)).status is ApprovalStatus.PENDING
    expiries = [
        entry for entry in await audit_trail(control_database) if entry[0] == "approval.expired"
    ]
    assert len(expiries) == 3
    assert {(actor, context) for _, actor, _, context in expiries} == {("svc-sweeper", RUN)}
    with pytest.raises(ApprovalExpiredError):
        await queue.resolve(due[0].id, decision=Decision.APPROVE, principal=APPROVER)
    with pytest.raises(ApprovalExpiredError):
        await queue.cancel(due[0].id, principal=REQUESTER)


async def test_concurrent_sweeps_expire_each_request_once(
    control_database: ControlDatabase,
) -> None:
    clock = Clock()
    queue = split_queue(control_database, clock=clock)
    await expired_requests(queue, clock, 6)

    counts = await asyncio.gather(
        queue.requester.expire_due(principal=SWEEPER, limit=2),
        queue.approver.expire_due(principal=SWEEPER, limit=2),
    )

    assert sum(counts) == 6
    actions = [action for action, *_ in await audit_trail(control_database)]
    assert actions.count("approval.expired") == 6


async def test_the_sweep_waits_for_the_databases_clock(control_database: ControlDatabase) -> None:
    if control_database.backend == "sqlite":
        pytest.skip("SQLite has no clock of its own")
    queue = split_queue(control_database)
    request = await submit(queue)

    # The application's clock runs two hours ahead; the database's has not reached expiry.
    assert await queue.expire_due(principal=SWEEPER, now=NOW + timedelta(hours=2)) == 0
    assert control_database.raw(
        f"SELECT status FROM agent_core_approvals WHERE id = '{request.id}'"
    ) == [("pending",)]


async def test_a_naive_sweep_time_is_refused(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)

    with pytest.raises(ValueError, match="timezone-aware"):
        await queue.expire_due(principal=SWEEPER, now=datetime(2026, 10, 2, 12, 0))
