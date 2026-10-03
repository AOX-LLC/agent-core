"""purge_payloads: finished requests lose their stored payload, never their hash."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
)
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import install_postgres_schema
from databases import APPROVER_ROLE, REQUESTER_ROLE, ControlDatabase, SplitQueue, split_queue
from test_approval_payload import ACTION, APPROVALS
from test_approvals import APPROVER, REQUESTER

SWEEPER = Principal(id="svc-retention", kind=PrincipalKind.SERVICE)
GUARD = "agent_core_approvals_guard"
TIME_COLUMNS = ("created_at", "expires_at", "resolved_at", "consumed_at", "closed_at")


def age(database: ControlDatabase, request_id: UUID, by: timedelta) -> None:
    """Move every timestamp of a request back, as the owner: it finished `by` earlier."""
    rows = database.raw(
        f"SELECT {', '.join(TIME_COLUMNS)} FROM {APPROVALS} WHERE id = '{request_id}'"
    )
    moved = {
        column: canonical_timestamp(datetime.fromisoformat(value) - by)
        for column, value in zip(TIME_COLUMNS, rows[0], strict=True)
        if value is not None
    }
    assignments = ", ".join(f"{column} = '{value}'" for column, value in moved.items())
    update = f"UPDATE {APPROVALS} SET {assignments} WHERE id = '{request_id}'"
    if database.backend == "postgres":
        database.raw(f"ALTER TABLE {APPROVALS} DISABLE TRIGGER {GUARD}")
        database.raw(update)
        database.raw(f"ALTER TABLE {APPROVALS} ENABLE TRIGGER {GUARD}")
    else:
        database.raw(update)


async def make(queue: SplitQueue, number: int, **options: Any) -> ApprovalRequest:
    terms: dict[str, Any] = {
        "action": ACTION,
        "summary": f"Update contact {number}",
        "payload": {"contact_id": f"c-{number}", "phone": "+1-555-0100"},
        "requested_by": REQUESTER,
        "required_role": "ops.approver",
        "ttl_seconds": 3_600,
        "include_payload": True,
    }
    return await queue.submit(**{**terms, **options})


async def finish(queue: SplitQueue, request: ApprovalRequest, how: str) -> None:
    if how == "consumed":
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
        await queue.consume(
            request.id, action=ACTION, payload=request_payload(request), principal=REQUESTER
        )
    elif how == "rejected":
        await queue.resolve(request.id, decision=Decision.REJECT, principal=APPROVER, reason="no")
    elif how == "cancelled":
        await queue.cancel(request.id, principal=REQUESTER)


def request_payload(request: ApprovalRequest) -> dict[str, Any]:
    assert request.payload is not None
    return dict(request.payload)


async def audit_actions(database: ControlDatabase) -> list[tuple[str, str]]:
    log = SQLAuditLog(database.database)
    return [(r.action, r.actor_id) async for r in log.iter_records()]


async def test_purge_drops_finished_old_payloads_and_nothing_else(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    old = timedelta(days=3)
    consumed, rejected, cancelled = (
        await make(queue, 1),
        await make(queue, 2),
        await make(queue, 3),
    )
    for request, how in ((consumed, "consumed"), (rejected, "rejected"), (cancelled, "cancelled")):
        await finish(queue, request, how)
    lapsed = await make(queue, 4, ttl_seconds=1)
    await asyncio.sleep(1.3)
    assert await queue.expire_due(principal=SWEEPER) == 1
    pending, approved = await make(queue, 5), await make(queue, 6)
    await queue.resolve(approved.id, decision=Decision.APPROVE, principal=APPROVER)
    recent = await make(queue, 7)
    await finish(queue, recent, "consumed")
    unstored = await make(queue, 8, include_payload=False)
    await finish(queue, unstored, "cancelled")
    for request in (consumed, rejected, cancelled, lapsed, pending, approved, unstored):
        age(control_database, request.id, old)

    purged = await queue.approver.purge_payloads(principal=SWEEPER, older_than=timedelta(days=2))

    assert purged == 4
    for request in (consumed, rejected, cancelled, lapsed):
        read = await queue.get(request.id)
        assert read.payload is None
        assert read.payload_purged_at is not None
        assert read.payload_sha256 == request.payload_sha256
        assert read.status is not ApprovalStatus.PENDING
    # Not finished, too recent, or never stored: untouched, and told apart.
    assert (await queue.get(pending.id)).payload == request_payload(pending)
    assert (await queue.get(approved.id)).payload == request_payload(approved)
    assert (await queue.get(recent.id)).payload == request_payload(recent)
    never = await queue.get(unstored.id)
    assert (never.payload, never.payload_purged_at) == (None, None)
    assert [e for e in await audit_actions(control_database) if e[0].endswith("purged")] == [
        ("approval.payload_purged", SWEEPER.id)
    ] * 4
    # A second run finds nothing, and the hash column was never written.
    assert await queue.approver.purge_payloads(principal=SWEEPER, older_than=old) == 0
    hashes = control_database.raw(
        f"SELECT payload_sha256 FROM {APPROVALS} WHERE id = '{consumed.id}'"
    )
    assert hashes == [(consumed.payload_sha256,)]


async def test_purge_works_in_batches(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)
    requests = [await make(queue, number) for number in range(5)]
    for request in requests:
        await finish(queue, request, "cancelled")
        age(control_database, request.id, timedelta(days=3))

    purged = await queue.approver.purge_payloads(
        principal=SWEEPER, older_than=timedelta(days=2), limit=2
    )

    assert purged == 5
    left = control_database.raw(f"SELECT count(*) FROM {APPROVALS} WHERE payload_json IS NOT NULL")
    assert left == [(0,)]


async def test_only_the_approver_side_purges(control_database: ControlDatabase) -> None:
    if control_database.backend == "sqlite":
        pytest.skip("SQLite has no sides")
    queue = split_queue(control_database)

    with pytest.raises(ConfigError, match="cannot purge payloads"):
        await queue.requester.purge_payloads(principal=SWEEPER, older_than=timedelta(days=2))


@pytest.mark.parametrize("older_than", [timedelta(0), timedelta(seconds=-5)])
async def test_the_retention_must_be_positive(
    control_database: ControlDatabase, older_than: timedelta
) -> None:
    queue = split_queue(control_database)

    with pytest.raises(ValueError, match="positive"):
        await queue.approver.purge_payloads(principal=SWEEPER, older_than=older_than)
    with pytest.raises(ValueError, match="at least 1"):
        await queue.approver.purge_payloads(
            principal=SWEEPER, older_than=timedelta(days=2), limit=0
        )


async def test_the_installed_floor_bounds_a_purge(control_database: ControlDatabase) -> None:
    if control_database.owner_url is None:
        pytest.skip("the floor is a Postgres guard setting")
    queue = split_queue(control_database)
    request = await make(queue, 1)
    await finish(queue, request, "cancelled")
    age(control_database, request.id, timedelta(hours=2))

    with pytest.raises(ValueError, match="retention floor of 86400 seconds"):
        await queue.approver.purge_payloads(principal=SWEEPER, older_than=timedelta(hours=1))
    assert (await queue.get(request.id)).payload is not None

    install_postgres_schema(
        control_database.owner_url,
        requester_role=REQUESTER_ROLE,
        approver_role=APPROVER_ROLE,
        schema=control_database.schema,
        payload_retention_floor=timedelta(minutes=30),
    )
    assert (
        await queue.approver.purge_payloads(principal=SWEEPER, older_than=timedelta(hours=1)) == 1
    )


def test_the_installer_gives_the_approver_the_purge_columns_once_not_on_every_run(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("grants are Postgres")
    can = (
        "SELECT has_column_privilege('{role}', 'public.agent_core_approvals', "
        "'payload_json', 'UPDATE')"
    )
    assert control_database.raw(can.format(role=APPROVER_ROLE)) == [(True,)]
    assert control_database.raw(can.format(role=REQUESTER_ROLE)) == [(False,)]

    # An operator who revoked the right (a legal hold, say) keeps it revoked on a re-run.
    control_database.raw(
        f"REVOKE UPDATE (payload_json, payload_purged_at) ON {APPROVALS} FROM {APPROVER_ROLE}"
    )
    install_postgres_schema(
        control_database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
    )
    assert control_database.raw(can.format(role=APPROVER_ROLE)) == [(False,)]


async def test_a_library_transaction_does_not_wait_unbounded_for_a_row_either(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None or control_database.owner_url is None:
        pytest.skip("row locks held by another role are Postgres")
    import time

    import psycopg

    from aox_agent_core.approvals import RoleApproverPolicy
    from aox_agent_core.approvals.sql import SQLApprovalQueue
    from databases import TEST_ACTION_ROLES

    queue = split_queue(control_database)
    request = await make(queue, 1)
    log = SQLAuditLog(
        control_database.approver_database,
        schema=control_database.schema,
        lock_timeout=timedelta(milliseconds=300),
    )
    approver = SQLApprovalQueue(
        control_database.approver_database,
        audit_log=log,
        schema=control_database.schema,
        policy=RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES),
    )

    with psycopg.connect(control_database.url) as holder:
        holder.execute(f"SELECT 1 FROM {APPROVALS} WHERE id = '{request.id}' FOR UPDATE")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await approver.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
        assert time.monotonic() - started < 3


class SlowRunClock:
    """The queue's clock as a purge run sees it when the run began six minutes ago.

    Each reading is a second after the last. A run over a large backlog reads the clock
    at its start and writes minutes later; the database's own clock has moved on.
    """

    def __init__(self) -> None:
        self.reading = datetime.now(UTC) - timedelta(minutes=6)

    def __call__(self) -> datetime:
        self.reading += timedelta(seconds=1)
        return self.reading


async def test_a_purge_whose_run_outlasts_the_five_minute_bound_still_completes(
    control_database: ControlDatabase,
) -> None:
    clock = SlowRunClock()
    queue = split_queue(control_database, clock=clock)
    requests = [await make(queue, number, ttl_seconds=3_600) for number in range(3)]
    for request in requests:
        await queue.cancel(request.id, principal=REQUESTER)
        age(control_database, request.id, timedelta(days=3))

    purged = await queue.approver.purge_payloads(
        principal=SWEEPER, older_than=timedelta(days=2), limit=1
    )

    assert purged == 3
    stamps = control_database.raw(f"SELECT payload_purged_at FROM {APPROVALS}")
    purged_at = [datetime.fromisoformat(value) for (value,) in stamps]
    assert len(purged_at) == 3
    if control_database.backend == "postgres":
        # The guard wrote them, by the database's clock, not the run's.
        assert all(abs(datetime.now(UTC) - moment) < timedelta(minutes=1) for moment in purged_at)
    else:
        # No guard on SQLite: each batch stamps its own reading of the application clock.
        assert len(set(purged_at)) == 3
