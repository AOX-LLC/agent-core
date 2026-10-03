"""The audit log on SQLite and Postgres: append-only, chained and checkable."""

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from aox_agent_core.approvals import Decision, Principal, PrincipalKind
from aox_agent_core.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditHead,
    AuditRecord,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.audit import sql as audit_sql
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import AuditIntegrityError, AuditPayloadRejectedError, ConfigError
from aox_agent_core.storage import open_database
from databases import (
    APPROVER_ROLE,
    REQUESTER_ROLE,
    ControlDatabase,
    split_queue,
    sqlite_database,
)

REFUSED = (sqlite3.DatabaseError, psycopg.Error)


def event(number: int) -> AuditEvent:
    return AuditEvent(
        action="model.call",
        actor_id="svc-triage",
        subject_id=f"ticket-{number}",
        payload={"input_tokens": 100 + number, "tier": "small"},
    )


async def filled_log(database: ControlDatabase, count: int = 3) -> SQLAuditLog:
    log = SQLAuditLog(database.database)
    for number in range(1, count + 1):
        await log.append(event(number))
    return log


def drop_triggers(database: ControlDatabase) -> None:
    if database.backend == "sqlite":
        database.raw(f"DROP TRIGGER {audit_sql.UPDATE_TRIGGER}")
        database.raw(f"DROP TRIGGER {audit_sql.DELETE_TRIGGER}")
    else:
        table = audit_sql.AUDIT_TABLE
        database.raw(f"DROP TRIGGER {audit_sql.UPDATE_DELETE_TRIGGER} ON {table}")
        database.raw(f"DROP TRIGGER {audit_sql.TRUNCATE_TRIGGER} ON {table}")


async def test_appends_form_a_gapless_verified_chain(control_database: ControlDatabase) -> None:
    log = await filled_log(control_database, 3)

    records = [record async for record in log.iter_records()]
    head = await log.verify()

    assert [record.seq for record in records] == [1, 2, 3]
    assert records[0].prev_hash == GENESIS_HASH
    assert [record.prev_hash for record in records[1:]] == [r.record_hash for r in records[:-1]]
    assert all(compute_record_hash(record) == record.record_hash for record in records)
    assert head == await log.head() == AuditHead(seq=3, record_hash=records[-1].record_hash)


async def test_iter_records_pages_and_resumes(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_sql, "READ_BATCH_SIZE", 2)
    log = await filled_log(control_database, 5)

    assert [record.seq async for record in log.iter_records(after_seq=1)] == [2, 3, 4, 5]


async def test_concurrent_appends_stay_gapless(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)

    await asyncio.gather(*(log.append(event(number)) for number in range(20)))

    assert (await log.verify()).seq == 20


@pytest.mark.parametrize(
    "statement",
    [
        f"UPDATE {audit_sql.AUDIT_TABLE} SET action = 'model.erased' WHERE seq = 2",
        f"DELETE FROM {audit_sql.AUDIT_TABLE} WHERE seq = 3",
    ],
)
async def test_raw_update_and_delete_are_refused(
    control_database: ControlDatabase, statement: str
) -> None:
    log = await filled_log(control_database)

    with pytest.raises(REFUSED, match="append-only"):
        control_database.raw(statement)  # the owner on Postgres: blocked by the trigger

    assert (await log.verify()).seq == 3


async def test_postgres_truncate_is_refused(control_database: ControlDatabase) -> None:
    if control_database.backend != "postgres":
        pytest.skip("TRUNCATE exists only on Postgres")
    await filled_log(control_database)

    with pytest.raises(psycopg.Error, match="append-only"):
        control_database.raw(f"TRUNCATE {audit_sql.AUDIT_TABLE}")


async def test_postgres_app_role_lacks_update_rights(control_database: ControlDatabase) -> None:
    if control_database.backend != "postgres":
        pytest.skip("roles exist only on Postgres")
    await filled_log(control_database)

    with (
        psycopg.connect(control_database.url, autocommit=True) as app,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        app.execute(f"UPDATE {audit_sql.AUDIT_TABLE} SET action = 'x.y'")


async def test_row_edited_with_triggers_dropped_is_caught(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database)
    drop_triggers(control_database)

    control_database.superuser_raw(
        f"UPDATE {audit_sql.AUDIT_TABLE} SET actor_id = 'someone-else' WHERE seq = 2"
    )

    with pytest.raises(AuditIntegrityError, match="Record 2 was altered"):
        await log.verify()


async def test_writing_stops_once_the_triggers_are_gone(control_database: ControlDatabase) -> None:
    await filled_log(control_database)
    drop_triggers(control_database)

    with pytest.raises(AuditIntegrityError, match="triggers are missing"):
        await SQLAuditLog(control_database.database).append(event(4))


async def test_truncated_tail_is_caught_only_with_an_anchor(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 4)
    anchor = await log.head()
    drop_triggers(control_database)

    control_database.superuser_raw(f"DELETE FROM {audit_sql.AUDIT_TABLE} WHERE seq = 4")

    assert (await log.verify()).seq == 3  # the chain alone still looks fine
    with pytest.raises(AuditIntegrityError, match="removed from the end"):
        await log.verify(expected_head=anchor)


async def test_rebuilt_chain_is_caught_only_with_an_anchor(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)
    log = await filled_log(database, 3)
    anchor = await log.head()
    drop_triggers(database)

    _rewrite_actor_and_rehash(database, seq=2, actor_id="someone-else")

    assert (await log.verify()).seq == 3
    with pytest.raises(AuditIntegrityError, match="Record 3 no longer matches the anchor"):
        await log.verify(expected_head=anchor)


def _rewrite_actor_and_rehash(database: ControlDatabase, *, seq: int, actor_id: str) -> None:
    """What an attacker with full write access does: edit a row, then re-hash the rest."""
    rows = database.raw(f"SELECT {audit_sql.COLUMNS} FROM {audit_sql.AUDIT_TABLE} ORDER BY seq")
    records = [audit_sql.record_from_row(row) for row in rows]
    prev_hash = records[seq - 2].record_hash if seq > 1 else GENESIS_HASH
    for record in records[seq - 1 :]:
        changes: dict[str, Any] = {"prev_hash": prev_hash}
        if record.seq == seq:
            changes["actor_id"] = actor_id
        unsealed = UnsealedAuditRecord(
            **{**record.model_dump(exclude={"record_hash", "db_role", "recorded_at"}), **changes}
        )
        record_hash = compute_record_hash(unsealed)
        database.raw(
            f"UPDATE {audit_sql.AUDIT_TABLE} SET actor_id = '{unsealed.actor_id}', "
            f"prev_hash = '{prev_hash}', record_hash = '{record_hash}' WHERE seq = {record.seq}"
        )
        prev_hash = record_hash


async def test_anchor_matching_the_log_passes(control_database: ControlDatabase) -> None:
    log = await filled_log(control_database, 2)
    anchor = await log.head()
    await log.append(event(3))

    assert (await log.verify(expected_head=anchor)).seq == 3
    assert (await log.verify(expected_head=AuditHead(seq=0, record_hash=GENESIS_HASH))).seq == 3


async def test_owner_and_superuser_roles_are_refused(control_database: ControlDatabase) -> None:
    if control_database.backend != "postgres":
        pytest.skip("roles exist only on Postgres")
    assert control_database.owner_url is not None
    assert control_database.superuser_url is not None

    for url in (control_database.owner_url, control_database.superuser_url):
        with pytest.raises(ConfigError, match="may only INSERT and SELECT"):
            await SQLAuditLog(open_database(url)).append(event(1))


async def test_secret_in_a_payload_is_rejected(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)
    leaky = AuditEvent(
        action="model.call", actor_id="svc-triage", payload={"note": "sk-ant-" + "x" * 24}
    )

    with pytest.raises(AuditPayloadRejectedError, match="anthropic_api_key"):
        await log.append(leaky)

    assert (await log.head()).seq == 0


async def test_payload_changed_after_construction_is_rejected(
    control_database: ControlDatabase,
) -> None:
    tampered = event(1)
    tampered.payload["api_key"] = "x"

    with pytest.raises(AuditPayloadRejectedError, match="changed after it was built"):
        await SQLAuditLog(control_database.database).append(tampered)


def test_timestamps_keep_six_fractional_digits() -> None:
    assert canonical_timestamp(datetime(2026, 10, 2, 8, 0, tzinfo=UTC)) == (
        "2026-10-02T08:00:00.000000Z"
    )


@pytest.mark.parametrize(
    "grants",
    [
        ("GRANT agent_core_owner TO {role}",),
        (
            f"GRANT SELECT, INSERT ON {audit_sql.AUDIT_TABLE} TO {{role}}",
            f"GRANT UPDATE (payload) ON {audit_sql.AUDIT_TABLE} TO {{role}}",
        ),
    ],
    ids=["member-of-owner-without-inherit", "column-level-update"],
)
async def test_roles_that_could_change_the_table_are_refused(
    control_database: ControlDatabase, grants: tuple[str, ...]
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("roles exist only on Postgres")
    url = control_database.login_role(*grants)

    with pytest.raises(ConfigError, match="may only INSERT and SELECT"):
        await SQLAuditLog(open_database(url)).append(event(1))


SET_REPEATABLE_READ_DEFAULT = """
DO $$ BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET default_transaction_isolation = ''repeatable read''',
        current_database()
    );
END $$
"""


async def test_appends_stay_safe_when_the_server_defaults_to_repeatable_read(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("isolation levels are a Postgres concern here")
    control_database.superuser_raw(SET_REPEATABLE_READ_DEFAULT)
    log = SQLAuditLog(control_database.database)

    await asyncio.gather(*(log.append(event(number)) for number in range(10)))

    assert (await log.verify()).seq == 10


async def test_verify_walks_in_batches_off_the_event_loop(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_sql, "READ_BATCH_SIZE", 2)
    log = await filled_log(control_database, 5)
    threads: set[str] = set()
    hash_record = compute_record_hash

    def hash_and_note_thread(record: UnsealedAuditRecord) -> str:
        threads.add(threading.current_thread().name)
        return hash_record(record)

    monkeypatch.setattr(audit_sql, "compute_record_hash", hash_and_note_thread)
    drop_triggers(control_database)
    control_database.superuser_raw(
        f"UPDATE {audit_sql.AUDIT_TABLE} SET actor_id = 'someone-else' WHERE seq = 5"
    )

    with pytest.raises(AuditIntegrityError, match="Record 5 was altered"):
        await log.verify()
    assert threading.main_thread().name not in threads


@pytest.mark.parametrize(
    "statement",
    [
        f"INSERT OR REPLACE INTO {audit_sql.AUDIT_TABLE} ({audit_sql.COLUMNS}) "
        "SELECT seq, schema_version, event_id, occurred_at, 'model.forged', actor_id, "
        "subject_id, payload, run_context, prev_hash, record_hash, db_role, recorded_at "
        f"FROM {audit_sql.AUDIT_TABLE} WHERE seq = 2",
        f"INSERT INTO {audit_sql.AUDIT_TABLE} ({audit_sql.COLUMNS}) "
        "SELECT 10, schema_version, 'forged-event', occurred_at, action, actor_id, "
        "subject_id, payload, run_context, prev_hash, record_hash, db_role, recorded_at "
        f"FROM {audit_sql.AUDIT_TABLE} WHERE seq = 3",
    ],
    ids=["insert-or-replace", "insert-out-of-order"],
)
async def test_inserts_land_only_at_the_end(
    control_database: ControlDatabase, statement: str
) -> None:
    if control_database.backend == "postgres" and "OR REPLACE" in statement:
        pytest.skip("INSERT OR REPLACE is SQLite syntax")
    log = await filled_log(control_database)

    with pytest.raises(REFUSED, match="append-only"):
        control_database.raw(statement)

    assert (await log.verify()).seq == 3


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ({"count": 2**53}, "within"),
        ({"items": [-(2**53)]}, "within"),
        ({"note": "\ud800"}, "not valid Unicode"),
    ],
)
def test_payloads_that_could_not_be_verified_everywhere_are_rejected(
    payload: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        AuditEvent(action="model.call", actor_id="svc-1", payload=payload)


def test_largest_safe_integer_and_booleans_are_fine() -> None:
    event = AuditEvent(action="model.call", actor_id="svc-1", payload={"n": 2**53 - 1, "ok": True})

    assert event.payload["n"] == 2**53 - 1


@pytest.mark.parametrize("url", ["sqlite:///:memory:", "sqlite:///file::memory:?cache=shared"])
def test_in_memory_sqlite_is_refused(url: str) -> None:
    with pytest.raises(ConfigError, match="in-memory"):
        open_database(url)


async def test_superuser_session_wearing_the_app_role_is_refused(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("roles exist only on Postgres")
    assert control_database.superuser_url is not None
    url = f"{control_database.superuser_url}?options=-c%20role%3Dagent_core_app"

    with pytest.raises(ConfigError, match="may only INSERT and SELECT"):
        await SQLAuditLog(open_database(url)).append(event(1))


async def test_insert_or_replace_on_an_existing_event_id_is_refused(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)
    log = await filled_log(database)

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        database.raw(
            f"INSERT OR REPLACE INTO {audit_sql.AUDIT_TABLE} ({audit_sql.COLUMNS}) "
            "SELECT 4, schema_version, event_id, occurred_at, action, actor_id, subject_id, "
            "payload, run_context, prev_hash, record_hash, db_role, recorded_at "
            f"FROM {audit_sql.AUDIT_TABLE} WHERE seq = 2"
        )

    assert (await log.verify()).seq == 3


async def test_a_temporary_table_cannot_stand_in_for_the_audit_table(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("search_path shadowing is a Postgres concern")
    await filled_log(control_database)

    forge_after_gap = (
        f"INSERT INTO public.{audit_sql.AUDIT_TABLE} ({audit_sql.COLUMNS}) "
        "SELECT 1001, schema_version, 'forged-event', occurred_at, action, actor_id, "
        "subject_id, payload, run_context, prev_hash, record_hash, db_role, recorded_at "
        f"FROM public.{audit_sql.AUDIT_TABLE} "
        "WHERE seq = 3"
    )

    with psycopg.connect(control_database.url, autocommit=True) as app:
        # Shadow the table with one whose MAX(seq) makes seq 1001 look like the next record.
        app.execute(f"CREATE TEMP TABLE {audit_sql.AUDIT_TABLE} AS SELECT 1000::bigint AS seq")
        with pytest.raises(psycopg.Error, match="append-only"):
            app.execute(forge_after_gap)


async def test_reading_a_missing_sqlite_file_does_not_create_it(tmp_path: Path) -> None:
    missing = tmp_path / "typo" / "audit.sqlite3"
    log = SQLAuditLog(open_database(f"sqlite:///{missing}"))

    assert (await log.verify()).seq == 0
    assert not missing.parent.exists()


async def test_a_write_sent_as_a_read_on_a_missing_file_fails_loudly(tmp_path: Path) -> None:
    database = open_database(f"sqlite:///{tmp_path / 'absent.sqlite3'}")

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        await database.run(lambda session: session.execute("CREATE TABLE stray (a INTEGER)"))


@pytest.mark.parametrize("version", [(3, 14, 2), (3, 34, 1)])
def test_a_sqlite_older_than_3_35_is_refused_with_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)

    with pytest.raises(ConfigError, match=r"needs 3\.35\.0 or later.*RETURNING.*use Postgres"):
        open_database(f"sqlite:///{tmp_path / 'audit.sqlite3'}")


def test_sqlite_3_35_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 35, 0))

    open_database(f"sqlite:///{tmp_path / 'audit.sqlite3'}")


# db_role: who wrote each row, as the database saw it


async def test_postgres_records_the_inserting_role(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)
    request = await queue.submit(
        action="crm.update_contact",
        summary="s",
        payload={},
        requested_by=Principal(id="agent-intake", kind=PrincipalKind.AGENT),
        required_role="ops.approver",
        ttl_seconds=600,
    )
    approver = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=approver)

    records = [record async for record in SQLAuditLog(control_database.database).iter_records()]
    expected = (
        [REQUESTER_ROLE, APPROVER_ROLE] if control_database.backend == "postgres" else [None, None]
    )
    assert [record.db_role for record in records] == expected
    assert (await SQLAuditLog(control_database.database).verify()).seq == 2


def test_db_role_is_outside_the_hash() -> None:
    unsealed = UnsealedAuditRecord(
        seq=1,
        event_id=UUID("6f2b8a43-46bb-4f7e-9d43-27c3f2f0a6f1"),
        occurred_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        action="model.call",
        actor_id="svc",
        subject_id=None,
        payload={},
        run_context=None,
        prev_hash=GENESIS_HASH,
    )
    sealed = AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))

    assert compute_record_hash(sealed.model_copy(update={"db_role": "someone"})) == (
        sealed.record_hash
    )


async def test_an_a2_sqlite_file_gains_db_role_in_place(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)
    log = await filled_log(database, 2)
    database.raw(f"ALTER TABLE {audit_sql.AUDIT_TABLE} DROP COLUMN db_role")

    fresh = SQLAuditLog(database.database)
    await fresh.append(event(3))

    assert (await fresh.verify()).seq == 3
    assert [record.db_role async for record in log.iter_records()] == [None, None, None]


async def test_an_a2_postgres_table_asks_for_the_installer(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "postgres":
        pytest.skip("only Postgres refuses; SQLite upgrades its own file")
    control_database.raw(f"ALTER TABLE {audit_sql.AUDIT_TABLE} DROP COLUMN db_role")

    with pytest.raises(ConfigError, match=r"earlier agent-core .* install_postgres_schema"):
        await SQLAuditLog(control_database.database).append(event(1))
