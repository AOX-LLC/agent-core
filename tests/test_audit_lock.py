"""The append lock: bounded waits, one lock per schema, and the same key in library and trigger.

Postgres only: SQLite has no advisory locks.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import psycopg
import psycopg_pool
import pytest

from aox_agent_core import _postgres_schema as layout
from aox_agent_core.approvals import (
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
)
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.audit import AuditEvent
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import AuditLockTimeoutError, ConfigError
from aox_agent_core.storage import PostgresDatabase, install_postgres_schema
from databases import (
    APPROVER_ROLE,
    REQUESTER_ROLE,
    TEST_ACTION_ROLES,
    ControlDatabase,
    postgres_database,
)

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
PAYLOAD = {"contact_id": "c-1001"}


@pytest.fixture
def pg() -> Iterator[ControlDatabase]:
    with postgres_database() as database:
        yield database


@pytest.fixture
async def pool(pg: ControlDatabase) -> AsyncIterator[psycopg_pool.AsyncConnectionPool]:
    host_pool = psycopg_pool.AsyncConnectionPool(pg.url, min_size=1, max_size=4, open=False)
    await host_pool.open()
    yield host_pool
    await host_pool.close()


def event(number: int) -> AuditEvent:
    return AuditEvent(action="host.order_placed", actor_id="svc-orders", subject_id=f"o-{number}")


async def hold_lock(connection: Any, schema: str) -> None:
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (layout.audit_lock_name(schema),)
    )


async def test_an_append_waits_for_the_lock_no_longer_than_its_timeout(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool), lock_timeout=timedelta(milliseconds=300))
    await log.append(event(0))

    async with pool.connection() as holder, holder.transaction():
        await hold_lock(holder, "public")
        started = time.monotonic()
        with pytest.raises(AuditLockTimeoutError, match="300ms"):
            await log.append(event(1))
        waited = time.monotonic() - started
        with pytest.raises(AuditLockTimeoutError):
            await log.append_many([event(2), event(3)])

    assert 0.25 < waited < 3
    # Nothing was written, the lock is free again, and the chain carries on.
    assert (await log.head()).seq == 1
    assert (await log.append(event(4))).seq == 2
    assert (await log.verify()).seq == 2


async def test_the_default_wait_is_five_seconds_and_must_be_positive(
    pg: ControlDatabase,
) -> None:
    from aox_agent_core.audit.sql import DEFAULT_LOCK_TIMEOUT

    assert timedelta(seconds=5) == DEFAULT_LOCK_TIMEOUT
    with pytest.raises(ValueError, match="lock_timeout"):
        SQLAuditLog(pg.database, lock_timeout=timedelta(0))


async def test_a_blocked_decision_fails_closed_and_changes_nothing(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema, lock_timeout=timedelta(milliseconds=300))
    policy = RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES)
    queue = SQLApprovalQueue(database, audit_log=log, schema=pg.schema, policy=policy)
    request = await queue.submit(
        action="crm.update_contact",
        summary="s",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )
    approver_database = pg.approver_database
    approver_log = SQLAuditLog(
        approver_database, schema=pg.schema, lock_timeout=timedelta(milliseconds=300)
    )
    approver_queue = SQLApprovalQueue(
        approver_database, audit_log=approver_log, schema=pg.schema, policy=policy
    )

    async with pool.connection() as holder, holder.transaction():
        await hold_lock(holder, pg.schema)
        with pytest.raises(AuditLockTimeoutError):
            await approver_queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING
    resolved = await approver_queue.resolve(
        request.id, decision=Decision.APPROVE, principal=APPROVER
    )
    assert resolved.status is ApprovalStatus.APPROVED


async def test_the_hosts_own_lock_timeout_is_put_back_after_an_append(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool), lock_timeout=timedelta(seconds=2))

    async with pool.connection() as connection, connection.transaction():
        await connection.execute("SET LOCAL lock_timeout = '7s'")
        await log.append(event(1), connection=connection)
        cursor = await connection.execute("SELECT current_setting('lock_timeout')")
        assert await cursor.fetchone() == ("7s",)


# One lock per schema


async def test_two_schemas_in_one_database_do_not_wait_for_each_other(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    assert pg.owner_url is not None
    install_postgres_schema(
        pg.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE, schema="second"
    )
    database = PostgresDatabase.from_pool(pool)
    first = SQLAuditLog(database, lock_timeout=timedelta(milliseconds=300))
    second = SQLAuditLog(database, schema="second", lock_timeout=timedelta(milliseconds=300))

    async with pool.connection() as holder, holder.transaction():
        await hold_lock(holder, "public")
        # Held on public: public waits and times out, the other schema is not touched.
        with pytest.raises(AuditLockTimeoutError):
            await first.append(event(1))
        assert (await asyncio.wait_for(second.append(event(2)), timeout=2)).seq == 1


async def test_the_trigger_takes_the_same_lock_as_the_library(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    """A plain INSERT waits behind a library append in a host's open transaction."""
    assert pg.requester_raw is not None
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))
    first = await log.append(event(0))
    raw_insert = _insert_after(first.seq, first.record_hash)

    async with pool.connection() as host, host.transaction():
        await log.append(event(1), connection=host)  # holds the lock until the block ends
        plain = asyncio.create_task(asyncio.to_thread(pg.requester_raw, raw_insert))
        await asyncio.sleep(0.5)
        assert not plain.done(), "a plain INSERT must wait for the append lock"

    # The host committed; the plain insert then saw the new head and was refused.
    with pytest.raises(psycopg.Error, match="must follow the last one"):
        await asyncio.wait_for(plain, timeout=10)
    assert (await log.verify()).seq == 2


def _insert_after(seq: int, prev_hash: str) -> str:
    from test_audit_throughput import raw_insert

    return raw_insert(seq + 1, prev_hash)


async def test_an_audit_trigger_older_than_this_release_is_refused(
    pg: ControlDatabase,
) -> None:
    source = pg.superuser_raw(
        "SELECT prosrc FROM pg_proc WHERE proname = 'agent_core_audit_append_at_end'"
    )[0][0]
    old = source.replace("-- agent-core audit trigger revision", "-- an older revision")
    pg.superuser_raw(
        "CREATE OR REPLACE FUNCTION public.agent_core_audit_append_at_end() RETURNS trigger "
        "LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $body$"
        f"{old}$body$"
    )

    with pytest.raises(ConfigError, match=r"older than this release"):
        await SQLAuditLog(pg.database).append(event(1))


# The order locks are taken in


async def test_a_host_that_appended_first_and_the_queue_do_not_deadlock(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    """The host takes the append lock, then wants the request's row; the queue takes the row,
    then wants the append lock. Postgres would abort one of them with 40P01."""
    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema)
    policy = RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES)
    queue = SQLApprovalQueue(database, audit_log=log, schema=pg.schema, policy=policy)
    approver_queue = SQLApprovalQueue(
        pg.approver_database,
        audit_log=SQLAuditLog(pg.approver_database, schema=pg.schema),
        schema=pg.schema,
        policy=policy,
    )
    request = await queue.submit(
        action="crm.update_contact",
        summary="s",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )
    await approver_queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    consume: dict[str, Any] = {
        "action": "crm.update_contact",
        "payload": PAYLOAD,
        "principal": REQUESTER,
    }

    async def queues_own_consume() -> Any:
        await asyncio.sleep(0.3)  # after the host has the append lock
        return await queue.consume(request.id, **consume)

    async def hosts_transaction() -> Any:
        async with pool.connection() as host, host.transaction():
            await log.append(event(1), connection=host)
            await asyncio.sleep(0.8)  # the queue's consume now holds the row
            return await queue.consume(request.id, **consume, connection=host)

    outcomes = await asyncio.wait_for(
        asyncio.gather(queues_own_consume(), hosts_transaction(), return_exceptions=True),
        timeout=30,
    )

    deadlocks = [o for o in outcomes if isinstance(o, psycopg.errors.DeadlockDetected)]
    assert deadlocks == [], "the lock order deadlocked"
    winners = [o for o in outcomes if getattr(o, "status", None) is ApprovalStatus.CONSUMED]
    assert len(winners) == 1
