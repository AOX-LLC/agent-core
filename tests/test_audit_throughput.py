"""append_many, caller-supplied occurred_at, recorded_at and the chain-linkage trigger."""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from aox_agent_core.audit import (
    GENESIS_HASH,
    OCCURRED_AT_MAX_FUTURE,
    OCCURRED_AT_MAX_PAST,
    AuditEvent,
    AuditHead,
    compute_record_hash,
)
from aox_agent_core.audit import sql as audit_sql
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import AuditPayloadRejectedError, AuditTimeRejectedError
from databases import ControlDatabase

REFUSED = (sqlite3.DatabaseError, psycopg.Error)


def event(number: int, **fields: Any) -> AuditEvent:
    return AuditEvent(
        action="model.call",
        actor_id="svc-triage",
        subject_id=f"ticket-{number}",
        payload={"input_tokens": 100 + number},
        **fields,
    )


def raw_insert(seq: int, prev_hash: str, **columns: str) -> str:
    """An INSERT of a plausible record: only the chain link and what `columns` change varies."""
    values = {
        "schema_version": "3",
        "event_id": f"'00000000-0000-4000-8000-{seq:012d}'",
        "occurred_at": f"'{canonical_timestamp(datetime.now(UTC))}'",
        "action": "'model.call'",
        "actor_id": "'svc-attacker'",
        "payload": "'{}'",
        "prev_hash": f"'{prev_hash}'",
        "record_hash": f"'{'a' * 64}'",
        **columns,
    }
    return (
        f"INSERT INTO {audit_sql.AUDIT_TABLE} (seq, {', '.join(values)}) "
        f"VALUES ({seq}, {', '.join(values.values())})"
    )


async def test_append_many_writes_one_gapless_chain(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)
    await log.append(event(0))

    records = await log.append_many([event(number) for number in range(1, 6)])

    assert [record.seq for record in records] == [2, 3, 4, 5, 6]
    assert all(compute_record_hash(record) == record.record_hash for record in records)
    assert (await log.verify()).seq == 6
    stored = [record async for record in log.iter_records(after_seq=1)]
    assert [(r.seq, r.record_hash) for r in stored] == [(r.seq, r.record_hash) for r in records]


async def test_append_many_of_nothing_writes_nothing(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)

    assert await log.append_many([]) == []
    assert await log.head() == AuditHead(seq=0, record_hash=GENESIS_HASH)


async def test_append_many_is_all_or_nothing(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)
    secret = AuditEvent(
        action="model.call",
        actor_id="svc-triage",
        payload={"note": "key sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"},
    )

    with pytest.raises(AuditPayloadRejectedError, match="Event 2"):
        await log.append_many([event(0), event(1), secret])

    assert (await log.head()).seq == 0


async def test_append_many_refuses_a_batch_over_the_limit(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_sql, "MAX_APPEND_BATCH", 3)
    log = SQLAuditLog(control_database.database)

    with pytest.raises(ValueError, match="at most 3"):
        await log.append_many([event(number) for number in range(4)])


async def test_a_supplied_occurred_at_is_stored_and_hashed(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)
    happened = datetime.now(UTC).replace(microsecond=123456) - timedelta(hours=3)

    record = await log.append(event(1, occurred_at=happened))

    assert record.occurred_at == happened
    assert record.recorded_at is not None
    assert record.recorded_at - happened > timedelta(hours=2, minutes=59)
    stored = await anext(log.iter_records())
    assert stored.occurred_at == happened
    assert stored.recorded_at == record.recorded_at
    assert compute_record_hash(stored) == stored.record_hash
    # recorded_at is not in the hash: only occurred_at is.
    assert compute_record_hash(stored.model_copy(update={"recorded_at": None})) == (
        stored.record_hash
    )
    assert compute_record_hash(stored.model_copy(update={"occurred_at": happened})) == (
        stored.record_hash
    )
    assert (
        compute_record_hash(stored.model_copy(update={"occurred_at": happened + timedelta(1)}))
        != stored.record_hash
    )


async def test_without_occurred_at_the_database_clock_is_used(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)

    record = await log.append(event(1))

    assert record.recorded_at is not None
    assert abs(record.occurred_at - record.recorded_at) < timedelta(seconds=1)


@pytest.mark.parametrize(
    "offset",
    [OCCURRED_AT_MAX_FUTURE + timedelta(minutes=1), -OCCURRED_AT_MAX_PAST - timedelta(hours=1)],
)
async def test_an_occurred_at_far_from_the_database_clock_is_refused(
    control_database: ControlDatabase, offset: timedelta
) -> None:
    log = SQLAuditLog(control_database.database)

    with pytest.raises(AuditTimeRejectedError):
        await log.append(event(1, occurred_at=datetime.now(UTC) + offset))

    assert (await log.head()).seq == 0


async def test_an_occurred_at_just_inside_the_bounds_is_accepted(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)

    records = await log.append_many(
        [
            event(
                1, occurred_at=datetime.now(UTC) + OCCURRED_AT_MAX_FUTURE - timedelta(seconds=30)
            ),
            event(2, occurred_at=datetime.now(UTC) - OCCURRED_AT_MAX_PAST + timedelta(minutes=1)),
        ]
    )

    assert [record.seq for record in records] == [1, 2]


# The database refuses what the library would never send.


async def test_the_database_refuses_a_record_that_does_not_link_to_the_head(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)
    first = await log.append(event(1))
    insert = control_database.requester_raw or control_database.raw

    with pytest.raises(REFUSED, match="append-only"):
        insert(raw_insert(2, "b" * 64))  # the right seq, the wrong prev_hash
    with pytest.raises(REFUSED, match="append-only"):
        insert(raw_insert(3, first.record_hash))  # a gap
    with pytest.raises(REFUSED, match="append-only"):
        insert(raw_insert(1, GENESIS_HASH))  # a fork

    assert await log.head() == AuditHead(seq=1, record_hash=first.record_hash)
    insert(raw_insert(2, first.record_hash))  # correctly linked, wrong hash: verify's job
    assert (await log.head()).seq == 2


async def test_the_database_refuses_a_first_record_that_is_not_genesis_linked(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)
    await log.append(event(0))  # creates the table on SQLite
    control_database.superuser_raw("SELECT 1")
    insert = control_database.requester_raw or control_database.raw

    with pytest.raises(REFUSED, match="append-only"):
        insert(raw_insert(2, GENESIS_HASH))


async def test_the_database_sets_recorded_at_whatever_the_writer_sends(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("only Postgres sets recorded_at itself")
    log = SQLAuditLog(control_database.database)
    first = await log.append(event(1))
    assert control_database.requester_raw is not None

    control_database.requester_raw(
        raw_insert(2, first.record_hash, recorded_at="'1999-01-01T00:00:00.000000Z'")
    )

    rows = control_database.raw(
        f"SELECT recorded_at, db_role FROM {audit_sql.AUDIT_TABLE} WHERE seq = 2"
    )
    assert rows[0][0] > "2020"
    assert rows[0][1] == "agent_core_requester"


@pytest.mark.parametrize(
    "occurred_at",
    [
        "'2999-01-01T00:00:00.000000Z'",
        "'2000-01-01T00:00:00.000000Z'",
        "'2026-10-03 10:00:00'",
        "'2026-10-03T10:00:00Z'",
    ],
)
async def test_the_database_refuses_a_bad_occurred_at(
    control_database: ControlDatabase, occurred_at: str
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("the shape and bounds of occurred_at are enforced by the Postgres trigger")
    log = SQLAuditLog(control_database.database)
    first = await log.append(event(1))
    assert control_database.requester_raw is not None

    with pytest.raises(psycopg.Error, match=r"append-only|too far"):
        control_database.requester_raw(raw_insert(2, first.record_hash, occurred_at=occurred_at))


async def test_a_batch_in_one_statement_links_each_row_to_the_one_before(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("one multi-row INSERT is the Postgres path")
    log = SQLAuditLog(control_database.database)

    records = await log.append_many([event(number) for number in range(30)])

    assert [record.seq for record in records] == list(range(1, 31))
    assert (await log.verify()).seq == 30
