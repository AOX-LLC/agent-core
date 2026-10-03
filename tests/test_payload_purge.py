"""purge_payloads: finished requests lose their stored payload, never their hash."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from aox_agent_core import _postgres_schema as layout
from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
)
from aox_agent_core.approvals.sql import purgeable_select
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import Dialect, TableName, install_postgres_schema
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


BACKLOG = 40_000


def test_the_purge_scan_reads_a_large_due_backlog_through_its_index(
    control_database: ControlDatabase,
) -> None:
    """The library's own scan, not a copy of it, on a backlog far larger than one batch.

    The scan filters on a canonical finish time as well, and the index carries that
    condition; without it the planner read every due row and sorted them for each batch.
    """
    if control_database.superuser_url is None:
        pytest.skip("the plan is read on Postgres")
    table = TableName("agent_core_approvals", control_database.schema)
    columns = (
        "id, action, summary, payload_sha256, requested_by, required_role, created_at, "
        "expires_at, status, delegates, payload_json, payload_purged_at, closed_at"
    )
    stamp = (
        "to_char(timestamp '2026-01-01' + g * interval '1 second', "
        '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
    )
    created = "'2025-12-31T00:00:00.000000Z'"
    expires = "'2025-12-31T01:00:00.000000Z'"

    def rows(prefix: str, count: int, closed: str, payload: str, purged: str) -> str:
        return (
            f"INSERT INTO {table.sql} ({columns}) SELECT '{prefix}' || g, 'ops.update', 's', "
            f"md5('{prefix}' || g) || md5(g::text || '{prefix}'), 'agent-intake', 'ops.approver', "
            f"{created}, {expires}, 'cancelled', '[]', {payload}, {purged}, {closed} "
            f"FROM generate_series(1, {count}) g"
        )

    payload = "'{\"a\": 1}'"
    with psycopg.connect(control_database.superuser_url, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(rows("due", BACKLOG, stamp, payload, "NULL"))
        connection.execute(rows("odd", BACKLOG // 4, "'finished-' || g", payload, "NULL"))
        connection.execute(
            rows("new", BACKLOG // 4, "'2099-01-01T00:00:00.000000Z'", payload, "NULL")
        )
        connection.execute(
            rows(
                "gone",
                BACKLOG // 2,
                "'2026-01-01T00:00:00.000000Z'",
                "NULL",
                "'2026-02-01T00:00:00.000000Z'",
            )
        )
        connection.execute(f"ANALYZE {table.sql}")
        scan = purgeable_select(table, Dialect.POSTGRES).replace("?", "%s")
        plan = [
            line
            for (line,) in connection.execute(
                "EXPLAIN (ANALYZE, COSTS OFF) " + scan, (86_400.0, 500)
            ).fetchall()
        ]
        found = connection.execute(scan, (86_400.0, 500)).fetchall()

    shown = "\n".join(plan)
    assert f"Index Scan using {layout.PURGEABLE_INDEX}" in shown, shown
    assert "Sort" not in shown, shown
    assert "Bitmap" not in shown, shown
    assert len(found) == 500


def test_the_installer_replaces_the_purge_index_an_a5_install_left_behind(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None or control_database.superuser_url is None:
        pytest.skip("indexes are read on Postgres")
    table = f"{control_database.schema}.agent_core_approvals"
    control_database.raw(f"DROP INDEX {control_database.schema}.{layout.PURGEABLE_INDEX}")
    control_database.raw(
        f"CREATE INDEX {layout.LEGACY_PURGEABLE_INDEX} ON {table} "
        f"(({layout.FINISHED_AT_EXPRESSION}), id) WHERE {layout.PURGEABLE_PREDICATE}"
    )

    install_postgres_schema(
        control_database.owner_url,
        requester_role=REQUESTER_ROLE,
        approver_role=APPROVER_ROLE,
        schema=control_database.schema,
    )

    names = control_database.raw(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_core_approvals' "
        f"AND schemaname = '{control_database.schema}' "
        "AND indexname LIKE 'agent_core_approvals_pur%'"
    )
    assert names == [(layout.PURGEABLE_INDEX,)]


def approver_with_short_lock_timeout(control_database: ControlDatabase) -> Any:
    from aox_agent_core.approvals import RoleApproverPolicy
    from aox_agent_core.approvals.sql import SQLApprovalQueue
    from databases import TEST_ACTION_ROLES

    log = SQLAuditLog(
        control_database.approver_database,
        schema=control_database.schema,
        lock_timeout=timedelta(milliseconds=300),
    )
    return SQLApprovalQueue(
        control_database.approver_database,
        audit_log=log,
        schema=control_database.schema,
        policy=RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES),
    )


async def test_the_expiry_sweep_skips_a_request_another_transaction_holds(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("row locks held by another transaction are Postgres")
    import psycopg

    queue = split_queue(control_database)
    held, free = await make(queue, 1, ttl_seconds=1), await make(queue, 2, ttl_seconds=1)
    await asyncio.sleep(1.3)
    sweeper = approver_with_short_lock_timeout(control_database)

    with psycopg.connect(control_database.url) as holder:
        holder.execute(f"SELECT 1 FROM {APPROVALS} WHERE id = '{held.id}' FOR UPDATE")
        # The other request expires; the held one is left for the next sweep, not an error.
        assert await sweeper.expire_due(principal=SWEEPER) == 1
        statuses = control_database.raw(
            f"SELECT id, status FROM {APPROVALS} WHERE id IN ('{held.id}', '{free.id}')"
        )
        assert dict(statuses) == {str(held.id): "pending", str(free.id): "expired"}

    assert await sweeper.expire_due(principal=SWEEPER) == 1
    assert (await queue.get(held.id)).status is ApprovalStatus.EXPIRED


async def test_the_purge_skips_a_request_another_transaction_holds(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("row locks held by another transaction are Postgres")
    import psycopg

    queue = split_queue(control_database)
    held, free = await make(queue, 1), await make(queue, 2)
    for request in (held, free):
        await queue.cancel(request.id, principal=REQUESTER)
        age(control_database, request.id, timedelta(days=3))
    purger = approver_with_short_lock_timeout(control_database)

    with psycopg.connect(control_database.url) as holder:
        holder.execute(f"SELECT 1 FROM {APPROVALS} WHERE id = '{held.id}' FOR UPDATE")
        purged = await purger.purge_payloads(principal=SWEEPER, older_than=timedelta(days=2))
        assert purged == 1
        assert (await queue.get(held.id)).payload is not None
        assert (await queue.get(free.id)).payload is None

    assert await purger.purge_payloads(principal=SWEEPER, older_than=timedelta(days=2)) == 1
    assert (await queue.get(held.id)).payload is None


async def test_sqlite_drops_the_a5_purge_index_and_builds_the_new_one_on_first_use(
    tmp_path: Any,
) -> None:
    import sqlite3

    from aox_agent_core.approvals.sql import SQLApprovalQueue
    from aox_agent_core.storage import open_database

    path = tmp_path / "control.sqlite3"
    database = open_database(f"sqlite:///{path}")
    queue = SQLApprovalQueue(database, audit_log=SQLAuditLog(database))
    await queue.submit(
        action=ACTION,
        summary="s",
        payload={"a": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=60,
    )
    await database.aclose()
    with contextlib.closing(sqlite3.connect(path)) as raw:
        raw.execute(
            f"CREATE INDEX {layout.LEGACY_PURGEABLE_INDEX} ON agent_core_approvals "
            f"(({layout.FINISHED_AT_EXPRESSION}), id) WHERE {layout.PURGEABLE_PREDICATE}"
        )
        raw.execute(f"DROP INDEX {layout.PURGEABLE_INDEX}")
        raw.commit()
    reopened = open_database(f"sqlite:///{path}")

    await SQLApprovalQueue(reopened, audit_log=SQLAuditLog(reopened)).submit(
        action=ACTION,
        summary="s2",
        payload={"a": 2},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=60,
    )
    await reopened.aclose()

    with contextlib.closing(sqlite3.connect(path)) as raw:
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE '%purge%'"
            )
        }
    assert names == {layout.PURGEABLE_INDEX}
