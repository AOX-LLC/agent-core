"""The blocking facades: scripts call the async log and queue without an event loop."""

import asyncio
from pathlib import Path

import pytest

from aox_agent_core.approvals import Decision, Principal, PrincipalKind, RoleApproverPolicy
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.audit import AuditEvent, AuditHead
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import EventLoopRunningError
from aox_agent_core.storage import PostgresDatabase, open_database
from aox_agent_core.sync import SyncApprovalQueue, SyncAuditLog
from databases import TEST_ACTION_ROLES, ControlDatabase, postgres_database, sqlite_database

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))


def event(number: int) -> AuditEvent:
    return AuditEvent(action="model.call", actor_id="svc", subject_id=f"t-{number}")


def test_a_script_appends_and_verifies_without_an_event_loop(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)

    with SyncAuditLog(SQLAuditLog(database.database)) as log:
        first = log.append(event(1))
        batch = log.append_many([event(2), event(3)])
        head = log.head()

        assert [r.seq for r in batch] == [2, 3]
        assert head == AuditHead(seq=3, record_hash=batch[-1].record_hash)
        assert log.verify(expected_head=head) == head
        assert [r.seq for r in log.iter_records()] == [1, 2, 3]
        assert [r.seq for r in log.iter_records(after_seq=2)] == [3]
        assert first.seq == 1


def test_one_pool_serves_every_call_and_close_releases_it() -> None:
    with postgres_database() as pg:
        database = open_database(pg.url, max_connections=2)
        assert isinstance(database, PostgresDatabase)
        log = SyncAuditLog(SQLAuditLog(database))

        for number in range(10):
            log.append(event(number))

        assert database._owned.get_stats()["connections_num"] <= 2
        assert log.verify().seq == 10
        log.close()
        assert database._owned is None


def test_the_approval_queue_runs_its_whole_life_in_blocking_calls(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)
    audit = SQLAuditLog(database.database)
    queue = SQLApprovalQueue(
        database.database,
        audit_log=audit,
        policy=RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES),
    )
    payload = {"contact_id": "c-1001"}

    with SyncApprovalQueue(queue) as sync_queue:
        request = sync_queue.submit(
            action="crm.update_contact",
            summary="Update the sample contact",
            payload=payload,
            requested_by=REQUESTER,
            required_role="ops.approver",
            ttl_seconds=600,
            include_payload=True,
        )
        assert [r.id for r in sync_queue.list_pending(APPROVER)] == [request.id]
        sync_queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
        used = sync_queue.consume(
            request.id, action="crm.update_contact", payload=payload, principal=REQUESTER
        )
        assert used.payload is None  # consume checks nothing stored, so returns none
        assert sync_queue.get(request.id).payload == payload
        assert sync_queue.get(request.id).status.value == "consumed"
        assert sync_queue.expire_due(principal=REQUESTER) == 0


def test_a_facade_refuses_to_run_inside_an_event_loop(tmp_path: Path) -> None:
    log = SyncAuditLog(SQLAuditLog(sqlite_database(tmp_path).database))

    async def call() -> None:
        log.head()

    with pytest.raises(EventLoopRunningError):
        asyncio.run(call())
    log.close()


def test_closing_an_unused_facade_is_harmless(tmp_path: Path) -> None:
    log = SyncAuditLog(SQLAuditLog(sqlite_database(tmp_path).database))

    log.close()
    log.close()


def test_the_postgres_queue_works_through_the_facade() -> None:
    with postgres_database() as pg:
        _ = _queue_on(pg)
        queue = SyncApprovalQueue(_queue_on(pg))
        request = queue.submit(
            action="crm.update_contact",
            summary="Update",
            payload={"contact_id": "c-1"},
            requested_by=REQUESTER,
            required_role="ops.approver",
            ttl_seconds=600,
        )
        assert queue.get(request.id).id == request.id
        queue.close()


def _queue_on(pg: ControlDatabase) -> SQLApprovalQueue:
    return SQLApprovalQueue(
        open_database(pg.url),
        audit_log=SQLAuditLog(open_database(pg.url)),
        schema=pg.schema,
    )
