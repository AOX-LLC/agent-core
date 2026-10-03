"""A host's own pool and transaction: the library borrows connections and joins transactions.

Postgres only: SQLite has no pool and no connection a host could hand over.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg_pool
import pytest

from aox_agent_core.approvals import Decision, Principal, PrincipalKind, RoleApproverPolicy
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.audit import AuditEvent, AuditHead
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ConfigError, NotTheRequesterError
from aox_agent_core.storage import PostgresDatabase, open_database
from databases import (
    APPROVER_ROLE,
    TEST_ACTION_ROLES,
    ControlDatabase,
    postgres_database,
    sqlite_database,
)

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
STRANGER = Principal(id="agent-other", kind=PrincipalKind.AGENT)
PAYLOAD = {"contact_id": "c-1001"}


@pytest.fixture
def pg() -> Iterator[ControlDatabase]:
    with postgres_database() as database:
        database.raw("CREATE TABLE host_orders (id integer PRIMARY KEY)")
        database.raw("GRANT SELECT, INSERT ON host_orders TO agent_core_requester")
        yield database


@pytest.fixture
async def pool(pg: ControlDatabase) -> AsyncIterator[psycopg_pool.AsyncConnectionPool]:
    host_pool = psycopg_pool.AsyncConnectionPool(pg.url, min_size=1, max_size=3, open=False)
    await host_pool.open()
    yield host_pool
    await host_pool.close()


def event(number: int) -> AuditEvent:
    return AuditEvent(action="host.order_placed", actor_id="svc-orders", subject_id=f"o-{number}")


async def host_orders(pg: ControlDatabase) -> list[int]:
    return sorted(row[0] for row in pg.raw("SELECT id FROM host_orders"))


async def audit_actions(log: SQLAuditLog) -> list[str]:
    return [record.action async for record in log.iter_records()]


def queue_on(database: PostgresDatabase, pg: ControlDatabase, log: SQLAuditLog) -> SQLApprovalQueue:
    return SQLApprovalQueue(
        database,
        audit_log=log,
        schema=pg.schema,
        policy=RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES),
    )


async def submit(queue: SQLApprovalQueue, **options: Any) -> Any:
    return await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
        **options,
    )


# A host pool, and no connection per operation


async def test_the_library_uses_a_host_pool_and_never_opens_a_connection_per_call(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))

    for number in range(25):
        await log.append(event(number))

    assert (await log.verify()).seq == 25
    assert pool.get_stats()["connections_num"] <= 3
    assert pool.get_stats()["requests_num"] >= 25


async def test_a_pool_the_library_opens_is_opened_once_and_closed_by_aclose(
    pg: ControlDatabase,
) -> None:
    database = open_database(pg.url, max_connections=2)
    assert isinstance(database, PostgresDatabase)
    log = SQLAuditLog(database)

    for number in range(25):
        await log.append(event(number))

    stats = database._owned.get_stats()
    assert stats["connections_num"] <= 2
    await database.aclose()
    assert database._owned is None


async def test_a_host_pool_is_not_closed_by_aclose(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    database = PostgresDatabase.from_pool(pool)
    await SQLAuditLog(database).append(event(1))

    await database.aclose()

    assert not pool.closed
    await SQLAuditLog(PostgresDatabase.from_pool(pool)).append(event(2))


async def test_session_settings_left_on_a_pooled_connection_do_not_reach_the_library(
    pg: ControlDatabase,
) -> None:
    host_pool = psycopg_pool.AsyncConnectionPool(pg.url, min_size=1, max_size=1, open=False)
    await host_pool.open()
    try:
        async with host_pool.connection() as connection:
            await connection.set_autocommit(True)
            await connection.execute("SET search_path = public, pg_temp")
            await connection.execute(
                "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL SERIALIZABLE"
            )
            await connection.set_autocommit(False)
        database = PostgresDatabase.from_pool(host_pool)
        log = SQLAuditLog(database)

        async def seen(session: Any) -> tuple[str, str]:
            rows = await session.execute(
                "SELECT current_setting('transaction_isolation'), current_setting('search_path')"
            )
            isolation, path = rows[0]
            return str(isolation), str(path)

        isolation, path = await database.run(seen)
        await log.append(event(1))

        assert isolation == "read committed"
        assert path == "pg_catalog, pg_temp"
        assert (await log.verify()).seq == 1
    finally:
        await host_pool.close()


# Inside a transaction the host controls


async def test_the_hosts_write_and_its_audit_event_commit_together(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))

    async with pool.connection() as connection, connection.transaction():
        await connection.execute("INSERT INTO host_orders VALUES (1)")
        record = await log.append(event(1), connection=connection)

    assert await host_orders(pg) == [1]
    assert (await log.verify()).seq == 1
    assert [r.seq async for r in log.iter_records()] == [record.seq]


async def test_a_host_rollback_takes_the_audit_event_with_it(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))
    committed = await log.append(event(0))

    with pytest.raises(RuntimeError, match="host failed"):  # noqa: PT012 - the block is the host's whole transaction
        async with pool.connection() as connection, connection.transaction():
            await connection.execute("INSERT INTO host_orders VALUES (1)")
            await log.append_many([event(1), event(2)], connection=connection)
            raise RuntimeError("host failed")

    assert await host_orders(pg) == []
    assert await log.head() == AuditHead(seq=1, record_hash=committed.record_hash)
    # The next append reuses the sequence numbers the rolled-back ones held.
    assert (await log.append(event(3))).seq == 2
    assert (await log.verify()).seq == 2


async def test_a_host_rollback_takes_the_approval_and_its_event_with_it(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema)
    queue = queue_on(database, pg, log)

    with pytest.raises(RuntimeError, match="host failed"):  # noqa: PT012 - the block is the host's whole transaction
        async with pool.connection() as connection, connection.transaction():
            request = await submit(queue, connection=connection)
            assert (await queue.get(request.id, connection=connection)).id == request.id
            raise RuntimeError("host failed")

    assert pg.raw("SELECT count(*) FROM agent_core_approvals") == [(0,)]
    assert await audit_actions(log) == []

    async with pool.connection() as connection, connection.transaction():
        kept = await submit(queue, connection=connection)
    assert (await queue.get(kept.id)).id == kept.id
    assert await audit_actions(log) == ["approval.requested"]


async def test_the_search_path_is_the_hosts_again_after_the_librarys_call(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))

    async with pool.connection() as connection, connection.transaction():
        before = await (await connection.execute("SHOW search_path")).fetchone()
        await log.append(event(1), connection=connection)
        after = await (await connection.execute("SHOW search_path")).fetchone()
        await connection.execute("INSERT INTO host_orders VALUES (1)")  # resolves unqualified

    assert before == after
    assert await host_orders(pg) == [1]


async def test_the_library_refuses_a_connection_with_no_transaction_open(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))

    async with pool.connection() as connection:
        await connection.set_autocommit(True)
        with pytest.raises(ConfigError, match="not in a healthy open transaction"):
            await log.append(event(1), connection=connection)
        await connection.set_autocommit(False)

    assert (await log.head()).seq == 0


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
async def test_the_library_refuses_a_transaction_above_read_committed(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool, level: str
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))

    async with pool.connection() as connection, connection.transaction():
        await connection.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
        with pytest.raises(ConfigError, match="READ COMMITTED"):
            await log.append(event(1), connection=connection)

    assert (await log.head()).seq == 0


async def test_a_failure_inside_the_librarys_part_leaves_the_hosts_transaction_usable(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))
    bad = AuditEvent(action="model.call", actor_id="svc", payload={"note": "sk-ant-" + "x" * 30})

    async with pool.connection() as connection, connection.transaction():
        await connection.execute("INSERT INTO host_orders VALUES (1)")
        with pytest.raises(Exception, match=r"secret|anthropic"):
            await log.append(bad, connection=connection)
        await log.append(event(2), connection=connection)

    assert await host_orders(pg) == [1]
    assert (await log.verify()).seq == 1


async def test_the_connection_must_be_a_psycopg_async_connection(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    log = SQLAuditLog(PostgresDatabase.from_pool(pool))

    with pytest.raises(ConfigError, match="AsyncConnection"):
        await log.append(event(1), connection=object())


async def test_sqlite_has_no_connection_to_join(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)
    log = SQLAuditLog(database.database)

    with pytest.raises(ConfigError, match="needs Postgres"):
        await log.append(event(1), connection=object())


# A refusal outlives the host's rollback


async def test_a_refusal_is_audited_even_when_the_host_rolls_back(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema)
    queue = queue_on(database, pg, log)
    request = await submit(queue)

    with pytest.raises(NotTheRequesterError):  # noqa: PT012 - the block is the host's whole transaction
        async with pool.connection() as connection, connection.transaction():
            await connection.execute("INSERT INTO host_orders VALUES (1)")
            await queue.consume(
                request.id,
                action="crm.update_contact",
                payload=PAYLOAD,
                principal=STRANGER,
                connection=connection,
            )

    assert await host_orders(pg) == []  # the host's own write rolled back with its error
    assert await audit_actions(log) == ["approval.requested", "approval.consume_denied"]
    assert (await log.verify()).seq == 2


async def test_a_refusal_falls_back_to_the_hosts_transaction_when_the_host_holds_the_lock(
    pg: ControlDatabase,
    pool: psycopg_pool.AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aox_agent_core.approvals.sql.DENIAL_LOCK_TIMEOUT", "300ms")
    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema)
    queue = queue_on(database, pg, log)
    request = await submit(queue)

    async with pool.connection() as connection, connection.transaction():
        await log.append(event(1), connection=connection)  # the host holds the append lock now
        with pytest.raises(NotTheRequesterError):
            await queue.consume(
                request.id,
                action="crm.update_contact",
                payload=PAYLOAD,
                principal=STRANGER,
                connection=connection,
            )

    # No deadlock, and the refusal was recorded in the host's transaction, which committed.
    assert await audit_actions(log) == [
        "approval.requested",
        "host.order_placed",
        "approval.consume_denied",
    ]
    assert (await log.verify()).seq == 3


async def test_a_success_in_the_hosts_transaction_is_not_written_apart(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema)
    queue = queue_on(database, pg, log)
    request = await submit(queue)
    approver_database = open_database(pg.url.replace("agent_core_requester", APPROVER_ROLE))
    approver = SQLApprovalQueue(
        approver_database,
        audit_log=SQLAuditLog(approver_database, schema=pg.schema),
        schema=pg.schema,
        policy=RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES),
    )
    human = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
    await approver.resolve(request.id, decision=Decision.APPROVE, principal=human)

    with pytest.raises(RuntimeError, match="host failed"):  # noqa: PT012 - the block is the host's whole transaction
        async with pool.connection() as connection, connection.transaction():
            await queue.consume(
                request.id,
                action="crm.update_contact",
                payload=PAYLOAD,
                principal=REQUESTER,
                connection=connection,
            )
            raise RuntimeError("host failed")

    # The host rolled back, so the approval is still unused and nothing was audited.
    assert (await queue.get(request.id)).status.value == "approved"
    assert await audit_actions(log) == ["approval.requested", "approval.resolved"]


def test_the_version_check_refuses_a_server_older_than_16() -> None:
    import asyncio

    from aox_agent_core import _postgres_schema as layout

    class OldServer:
        async def execute(self, sql: str, parameters: Any = ()) -> list[tuple[Any, ...]]:
            return [("150012",)]

    with pytest.raises(ConfigError, match="needs 16 or later"):
        asyncio.run(layout.require_postgres_version(OldServer()))  # type: ignore[arg-type]


async def test_cancelling_an_append_that_waits_for_the_lock_leaves_the_pool_and_chain_sound(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    import asyncio

    from aox_agent_core import _postgres_schema

    log = SQLAuditLog(PostgresDatabase.from_pool(pool))
    await log.append(event(0))

    async with pool.connection() as holder, holder.transaction():
        await holder.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_postgres_schema.audit_lock_name("public"),),
        )
        waiting = asyncio.create_task(log.append(event(1)))
        await asyncio.sleep(0.3)  # it is now blocked on the lock the holder has
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    # The lock is free, the pool gave the connection back clean, and the chain is intact.
    record = await asyncio.wait_for(log.append(event(2)), timeout=10)
    assert record.seq == 2
    assert (await log.verify()).seq == 2
    leftover = pg.superuser_raw("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
    assert leftover == [(0,)]


async def test_a_log_on_another_database_object_is_refused_for_a_host_connection(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    queue = queue_on(
        PostgresDatabase.from_pool(pool), pg, SQLAuditLog(open_database(pg.url), schema=pg.schema)
    )

    with pytest.raises(ConfigError, match="shares this queue's database"):
        async with pool.connection() as connection, connection.transaction():
            await submit(queue, connection=connection)

    assert pg.raw("SELECT count(*) FROM agent_core_approvals") == [(0,)]
    assert pg.raw("SELECT count(*) FROM agent_core_audit") == [(0,)]


async def test_an_exhausted_pool_delays_a_refusal_briefly_then_falls_back_loudly(
    pg: ControlDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import time

    monkeypatch.setattr("aox_agent_core.approvals.sql.DENIAL_ACQUIRE_TIMEOUT", 0.3)
    one = psycopg_pool.AsyncConnectionPool(pg.url, min_size=1, max_size=1, open=False)
    await one.open()
    try:
        database = PostgresDatabase.from_pool(one)
        log = SQLAuditLog(database, schema=pg.schema)
        queue = queue_on(database, pg, log)
        request = await submit(queue)

        started = time.monotonic()
        async with one.connection() as connection, connection.transaction():
            with pytest.raises(NotTheRequesterError):
                await queue.consume(
                    request.id,
                    action="crm.update_contact",
                    payload=PAYLOAD,
                    principal=STRANGER,
                    connection=connection,
                )
        # The wait was bounded, the fallback was logged, and the refusal sits in the
        # host's transaction, which committed here because the host caught the error.
        assert time.monotonic() - started < 5
        assert "could not be audited apart" in caplog.text
        assert await audit_actions(log) == ["approval.requested", "approval.consume_denied"]
    finally:
        await one.close()


async def test_a_host_connection_or_pool_with_a_dict_row_factory_works(
    pg: ControlDatabase,
) -> None:
    from psycopg.rows import dict_row

    dict_pool = psycopg_pool.AsyncConnectionPool(
        pg.url, min_size=1, max_size=2, open=False, kwargs={"row_factory": dict_row}
    )
    await dict_pool.open()
    try:
        log = SQLAuditLog(PostgresDatabase.from_pool(dict_pool))
        await log.append(event(1))
        async with dict_pool.connection() as connection, connection.transaction():
            await log.append(event(2), connection=connection)

        assert (await log.verify()).seq == 2
    finally:
        await dict_pool.close()


class PlainPool:
    """A pool wrapper whose connection() takes nothing, exactly as ConnectionSource says."""

    def __init__(self, pool: psycopg_pool.AsyncConnectionPool) -> None:
        self._pool = pool

    def connection(self) -> Any:
        return self._pool.connection()


async def test_a_pool_whose_connection_takes_no_arguments_still_audits_a_refusal(
    pg: ControlDatabase, pool: psycopg_pool.AsyncConnectionPool
) -> None:
    database = PostgresDatabase.from_pool(PlainPool(pool))
    log = SQLAuditLog(database, schema=pg.schema)
    queue = queue_on(database, pg, log)
    request = await submit(queue)

    with pytest.raises(NotTheRequesterError):  # not a TypeError about timeout=
        async with pool.connection() as connection, connection.transaction():
            await queue.consume(
                request.id,
                action="crm.update_contact",
                payload=PAYLOAD,
                principal=STRANGER,
                connection=connection,
            )

    assert await audit_actions(log) == ["approval.requested", "approval.consume_denied"]


async def test_a_refusal_that_cannot_be_audited_anywhere_says_so_on_the_error_and_in_the_log(
    pg: ControlDatabase,
    pool: psycopg_pool.AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import psycopg

    database = PostgresDatabase.from_pool(pool)
    log = SQLAuditLog(database, schema=pg.schema)
    queue = queue_on(database, pg, log)
    request = await submit(queue)

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("the audit write failed")

    monkeypatch.setattr(SQLAuditLog, "append_many_in", fail)
    caught: NotTheRequesterError | None = None
    async with pool.connection() as connection, connection.transaction():
        try:
            await queue.consume(
                request.id,
                action="crm.update_contact",
                payload=PAYLOAD,
                principal=STRANGER,
                connection=connection,
            )
        except NotTheRequesterError as error:
            caught = error

    assert caught is not None
    assert any("was not audited" in note for note in caught.__notes__)
    assert "could not be audited at all" in caplog.text
    assert "approval.consume_denied" in caplog.text
    assert str(request.id) in caplog.text
