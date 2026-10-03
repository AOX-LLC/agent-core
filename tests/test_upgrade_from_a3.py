"""A Postgres schema installed by agent-core 0.1.0a3, with its rows, upgrades in place.

The fixture is what 0.1.0a3's installer created plus the rows 0.1.0a3 wrote: four
audit events and one pending approval. Each test replays it as the owner role into
an empty database, then runs this version's installer over it.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import JsonValue

from aox_agent_core.approvals import Principal, PrincipalKind
from aox_agent_core.audit import AuditEvent
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import install_postgres_schema
from databases import APPROVER_ROLE, REQUESTER_ROLE, ControlDatabase, postgres_database, split_queue

A3_SCHEMA = Path(__file__).parent / "fixtures" / "postgres" / "a3_schema.sql"
A3_AUDIT_SEQS = [1, 2, 3, 4]
A3_APPROVAL_ID = UUID("c2bfea0a-996d-4b4e-b97e-6eb4cae0241e")
TABLES = ("agent_core_audit", "agent_core_approvals", "agent_core_approval_roles")


def load_a3_schema(database: ControlDatabase) -> None:
    database.raw(A3_SCHEMA.read_text())


def upgrade(database: ControlDatabase) -> str:
    assert database.owner_url is not None
    report = install_postgres_schema(
        database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
    )
    return str(report)


def row_counts(database: ControlDatabase) -> dict[str, int]:
    return {table: database.raw(f"SELECT count(*) FROM {table}")[0][0] for table in TABLES}


def a4_event() -> AuditEvent:
    return AuditEvent(action="model.call", actor_id="svc-triage", subject_id="ticket-5")


async def test_an_a3_schema_refuses_writes_until_the_installer_runs() -> None:
    with postgres_database(install=False) as database:
        load_a3_schema(database)
        log = SQLAuditLog(database.database)

        with pytest.raises(ConfigError, match="install_postgres_schema"):
            await log.append(a4_event())

        assert [
            row[0] for row in database.raw("SELECT seq FROM agent_core_audit ORDER BY seq")
        ] == (A3_AUDIT_SEQS)


async def test_an_a3_schema_is_upgraded_in_place_and_keeps_verifying() -> None:
    with postgres_database(install=False) as database:
        load_a3_schema(database)
        before = database.raw(
            "SELECT seq, event_id, record_hash, prev_hash FROM agent_core_audit ORDER BY seq"
        )
        upgrade(database)
        log = SQLAuditLog(database.database)

        records = [record async for record in log.iter_records()]
        assert [record.seq for record in records] == A3_AUDIT_SEQS
        assert [
            (record.seq, str(record.event_id), record.record_hash, record.prev_hash)
            for record in records
        ] == [
            (seq, event_id, record_hash, prev_hash)
            for seq, event_id, record_hash, prev_hash in before
        ]
        assert all(record.recorded_at is None for record in records)
        assert all(record.db_role == REQUESTER_ROLE for record in records)
        assert (await log.verify()).seq == len(A3_AUDIT_SEQS)

        before_append = datetime.now(UTC)
        appended = [await log.append(a4_event()) for _ in range(2)]

        assert [record.seq for record in appended] == [5, 6]
        assert all(record.recorded_at is not None for record in appended)
        assert all(
            record.recorded_at is not None and record.recorded_at >= before_append
            for record in appended
        )
        assert appended[0].prev_hash == records[-1].record_hash
        assert appended[1].prev_hash == appended[0].record_hash
        every_record = [record async for record in log.iter_records()]
        assert [record.seq for record in every_record] == [1, 2, 3, 4, 5, 6]
        assert (await log.verify()).seq == 6


async def test_a3_approvals_stay_readable_and_new_ones_can_store_a_payload() -> None:
    with postgres_database(install=False) as database:
        load_a3_schema(database)
        upgrade(database)
        queue = split_queue(database)

        old = await queue.get(A3_APPROVAL_ID)
        assert old.action == "crm.update_contact"
        assert old.requested_by == "agent-intake"
        assert old.payload is None

        payload: dict[str, JsonValue] = {"contact_id": 17, "field": "phone"}
        request = await queue.submit(
            action="crm.update_contact",
            summary="Update contact 18",
            payload=payload,
            requested_by=Principal(id="agent-intake", kind=PrincipalKind.AGENT),
            required_role="ops.approver",
            ttl_seconds=600,
            include_payload=True,
        )
        assert (await queue.get(request.id)).payload == payload
        assert (await queue.get(A3_APPROVAL_ID)).payload is None
        assert await SQLAuditLog(database.database).verify()


async def test_running_the_installer_twice_changes_nothing() -> None:
    with postgres_database(install=False) as database:
        load_a3_schema(database)
        first = upgrade(database)
        assert "approval" not in first.lower().replace("approver role", "")
        counts = row_counts(database)
        assert counts == {
            "agent_core_audit": 4,
            "agent_core_approvals": 1,
            "agent_core_approval_roles": 1,
        }

        second = upgrade(database)

        assert second == first
        assert row_counts(database) == counts
        assert (await SQLAuditLog(database.database).verify()).seq == 4


async def test_the_upgraded_audit_trigger_refuses_a_record_that_does_not_follow_the_last() -> None:
    with postgres_database(install=False) as database:
        assert database.requester_raw is not None
        load_a3_schema(database)
        upgrade(database)
        head_hash = database.raw("SELECT record_hash FROM agent_core_audit WHERE seq = 4")[0][0]

        def insert(seq: int, prev_hash: str) -> str:
            return (
                "INSERT INTO agent_core_audit (seq, schema_version, event_id, occurred_at, action, "
                "actor_id, payload, prev_hash, record_hash) VALUES "
                f"({seq}, 3, '{uuid4()}', '{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}', "
                f"'model.call', 'svc-triage', '{{}}', '{prev_hash}', '{'b' * 64}')"
            )

        with pytest.raises(psycopg.Error, match="must follow the last one"):
            database.requester_raw(insert(5, "a" * 64))
        assert row_counts(database)["agent_core_audit"] == 4

        database.requester_raw(insert(5, head_hash))
        assert row_counts(database)["agent_core_audit"] == 5


async def test_an_a3_approvals_table_is_refused_by_the_queue_until_the_installer_runs() -> None:
    with postgres_database(install=False) as database:
        load_a3_schema(database)
        queue = split_queue(database)

        with pytest.raises(ConfigError, match=r"install_postgres_schema from 0\.1\.0a5"):
            await queue.submit(
                action="crm.update_contact",
                summary="s",
                payload={},
                requested_by=Principal(id="agent-intake", kind=PrincipalKind.AGENT),
                required_role="ops.approver",
                ttl_seconds=600,
            )

        assert row_counts(database)["agent_core_approvals"] == 1
