"""The audit log on SQLite or Postgres.

Append-only is enforced three ways, not promised:

- the API has no update or delete;
- the database refuses them: BEFORE UPDATE and BEFORE DELETE triggers on both
  backends, and a BEFORE TRUNCATE trigger on Postgres;
- on Postgres the application connects as a role that may only INSERT and
  SELECT, and the log refuses to write if that is not so.

The hash chain then detects what the database cannot prevent: an owner or
superuser who drops the triggers and edits rows. It cannot detect a rewritten
chain on its own; compare against a head kept elsewhere (verify's expected_head).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

from aox_agent_core import _postgres_schema as layout
from aox_agent_core._canonical import canonical_json
from aox_agent_core._validation import STORED_RECORD
from aox_agent_core.audit.chain import canonical_timestamp, compute_record_hash
from aox_agent_core.audit.types import (
    GENESIS_HASH,
    OCCURRED_AT_MAX_FUTURE,
    OCCURRED_AT_MAX_PAST,
    AuditEvent,
    AuditHead,
    AuditRecord,
    UnsealedAuditRecord,
)
from aox_agent_core.errors import (
    AuditIntegrityError,
    AuditLockTimeoutError,
    AuditPayloadRejectedError,
    AuditTimeRejectedError,
    ConfigError,
)
from aox_agent_core.replay.scrub import PatternScrubber, Scrubber
from aox_agent_core.storage import (
    Database,
    Dialect,
    Session,
    TableName,
    bring_table_up_to_date,
    driver_errors,
)

AUDIT_TABLE: Final = layout.AUDIT_TABLE
RUN_CONTEXT_COLUMN: Final = "run_context"
UPDATE_TRIGGER: Final = "agent_core_audit_no_update"
DELETE_TRIGGER: Final = "agent_core_audit_no_delete"
# The Postgres table, its triggers and grants are installed by
# storage.install_postgres_schema (see _postgres_schema).
UPDATE_DELETE_TRIGGER: Final = layout.AUDIT_UPDATE_DELETE_TRIGGER
TRUNCATE_TRIGGER: Final = layout.AUDIT_TRUNCATE_TRIGGER
APPEND_TRIGGER: Final = layout.AUDIT_APPEND_TRIGGER
SQLITE_TRIGGERS: Final = frozenset({UPDATE_TRIGGER, DELETE_TRIGGER, APPEND_TRIGGER})
POSTGRES_TRIGGERS: Final = layout.AUDIT_TRIGGERS

# How long a library-owned append waits for the append lock (see _postgres_schema): a role
# that holds it can stall writers only this long. Set per log with `lock_timeout`.
DEFAULT_LOCK_TIMEOUT: Final = timedelta(seconds=5)

READ_BATCH_SIZE = 500
# Most events one append_many takes: it holds the append lock for the whole batch.
MAX_APPEND_BATCH = 1000

COLUMNS = (
    "seq, schema_version, event_id, occurred_at, action, actor_id, subject_id, payload, "
    "run_context, prev_hash, record_hash, db_role, recorded_at"
)
# Columns added in 0.1.0a3 and 0.1.0a4, with their SQLite types.
ADDED_COLUMNS: Final = {"db_role": "TEXT", "recorded_at": "TEXT"}

_TABLE_DDL = f"""
CREATE TABLE {AUDIT_TABLE} (
    seq BIGINT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    subject_id TEXT,
    payload TEXT NOT NULL,
    run_context TEXT,
    prev_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    db_role TEXT,
    recorded_at TEXT
)"""

# Inserts land only right after the last record: the next seq, linked to the last
# record's hash, with an event_id not seen before. Besides keeping the chain
# gapless and linked, this stops INSERT OR REPLACE, which deletes the row it
# conflicts with (on seq or event_id) without firing the delete trigger.
_SQLITE_APPEND_CONDITION = f"""WHEN NEW.seq <> (SELECT COALESCE(MAX(seq), 0) FROM {AUDIT_TABLE}) + 1
        OR NEW.prev_hash <> COALESCE(
            (SELECT record_hash FROM {AUDIT_TABLE} ORDER BY seq DESC LIMIT 1), '{GENESIS_HASH}')
        OR EXISTS (SELECT 1 FROM {AUDIT_TABLE} WHERE event_id = NEW.event_id)"""

SQLITE_SCHEMA: Final = (
    _TABLE_DDL,
    f"""CREATE TRIGGER {UPDATE_TRIGGER} BEFORE UPDATE ON {AUDIT_TABLE}
    BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END""",
    f"""CREATE TRIGGER {DELETE_TRIGGER} BEFORE DELETE ON {AUDIT_TABLE}
    BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END""",
    f"""CREATE TRIGGER {APPEND_TRIGGER} BEFORE INSERT ON {AUDIT_TABLE}
    {_SQLITE_APPEND_CONDITION}
    BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END""",
)


class SQLAuditLog:
    """The AuditLog protocol on a Database.

    On SQLite the log creates its table and triggers on first write. On Postgres
    they are installed by the owner role (storage.install_postgres_schema), and
    the first write checks that the connecting role is not a superuser, does not
    own the table, and cannot UPDATE, DELETE or TRUNCATE it; otherwise it raises
    ConfigError and writes nothing. `schema` names the Postgres schema the table
    was installed in (public by default); SQLite has none.

    An append takes a database-wide advisory lock for its schema and waits for it at most
    `lock_timeout` (5 seconds by default) before raising AuditLockTimeoutError, writing
    nothing: a transaction that holds the lock, or a role that took it, stalls writers
    only that long. The bound covers the library's own append transactions; in a host's
    transaction (`connection=`) it is applied for the append and the host's own setting
    is put back after.
    """

    def __init__(
        self,
        database: Database,
        *,
        scrubber: Scrubber | None = None,
        schema: str | None = None,
        lock_timeout: timedelta = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if lock_timeout < timedelta(milliseconds=1):
            raise ValueError("lock_timeout must be at least a millisecond")
        self._lock_timeout = f"{int(lock_timeout.total_seconds() * 1000)}ms"
        self.database = database
        self._scrubber = scrubber if scrubber is not None else PatternScrubber()
        self._table = TableName.on(database, AUDIT_TABLE, schema)
        self._protections_checked = False

    async def append(self, event: AuditEvent, *, connection: Any = None) -> AuditRecord:
        """Validate and scrub the event, then add it to the chain in its own transaction.

        With `connection`, a psycopg AsyncConnection already in a transaction, the
        event is written inside that transaction instead, and committed or rolled
        back with it; see PostgresDatabase.run_on for what that requires.
        """
        return (await self.append_many([event], connection=connection))[0]

    async def append_many(
        self, events: Sequence[AuditEvent], *, connection: Any = None
    ) -> list[AuditRecord]:
        """Append every event in one transaction, under one lock, as consecutive records.

        All or nothing: every event is validated and scanned before anything is
        written, an error names the index of the event it refused, and a failure
        at any point stores none of them. At most MAX_APPEND_BATCH events. Returns
        the records in order; with `connection` they are provisional until the
        host's transaction commits.
        """
        if not events:
            return []
        if len(events) > MAX_APPEND_BATCH:
            raise ValueError(f"append_many takes at most {MAX_APPEND_BATCH} events")
        checked = [self._checked_at(index, event) for index, event in enumerate(events)]

        async def write(session: Session) -> list[AuditRecord]:
            return await self.append_many_in(session, checked)

        if connection is None:
            return await self.database.run(write, write=True)
        return await self.database.run_on(connection, write)

    def _checked_at(self, index: int, event: AuditEvent) -> AuditEvent:
        try:
            return self.checked_event(event)
        except AuditPayloadRejectedError as error:
            raise AuditPayloadRejectedError(f"Event {index}: {error}") from error

    def checked_event(self, event: AuditEvent) -> AuditEvent:
        """Re-validate an event and scan its payload strings for secrets.

        Raises AuditPayloadRejectedError if it is no longer valid or holds a secret.
        """
        try:
            revalidated = AuditEvent.model_validate(event.model_dump())
        except ValidationError as error:
            raise AuditPayloadRejectedError(
                "The event was changed after it was built and is no longer valid."
            ) from error
        scanned: dict[str, JsonValue] = {"payload": revalidated.payload}
        if revalidated.context is not None:
            scanned["context"] = revalidated.context.as_json()
        findings = self._scrubber.find_secrets(scanned)
        if findings:
            located = ", ".join(f"{finding.rule} at {finding.path}" for finding in findings)
            raise AuditPayloadRejectedError(f"The audit event contains {located}.")
        return revalidated

    async def append_in(self, session: Session, event: AuditEvent) -> AuditRecord:
        """Append within a write transaction on this log's database.

        For callers that must write their own change and its audit event together.
        The event must already have passed checked_event().
        """
        return (await self.append_many_in(session, [event]))[0]

    async def append_many_in(
        self, session: Session, events: Sequence[AuditEvent], *, lock_timeout: str | None = None
    ) -> list[AuditRecord]:
        """append_many within a write transaction on this log's database.

        The events must already have passed checked_event(). The caller holds the
        append lock from here until its transaction ends. The wait for it is bounded by
        this log's lock_timeout, or by `lock_timeout`, a Postgres interval such as "2s";
        AuditLockTimeoutError if it is not free in time.
        """
        await self._ensure_protected(session)
        previous_timeout: str | None = None
        if session.dialect is Dialect.POSTGRES:
            previous_timeout = await self._take_lock(session, lock_timeout or self._lock_timeout)
        head, database_now = await _head_and_now(session, self._table)
        for event in events:
            _check_occurred_at(event.occurred_at, database_now)

        records: list[AuditRecord] = []
        previous = head
        for event in events:
            unsealed = UnsealedAuditRecord(
                seq=previous.seq + 1,
                event_id=uuid4(),
                occurred_at=event.occurred_at if event.occurred_at is not None else database_now,
                action=event.action,
                actor_id=event.actor_id,
                subject_id=event.subject_id,
                payload=event.payload,
                run_context=event.context,
                prev_hash=previous.record_hash,
            )
            record = AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))
            records.append(record)
            previous = AuditHead(seq=record.seq, record_hash=record.record_hash)
        written = await self._insert(session, records, database_now)
        if previous_timeout is not None:
            # The host's own setting, back for the rest of its transaction.
            await session.execute("SELECT set_config('lock_timeout', ?, true)", (previous_timeout,))
        return written

    async def lock_in(self, session: Session) -> None:
        """Take the append lock now, in a write transaction on this log's database.

        For a caller that will append later in the same transaction and also changes other
        rows there: taking the lock first keeps the order (append lock, then rows) the same
        for everyone, which is what stops two such transactions from deadlocking. It is held
        until the transaction ends, and waited for at most this log's lock_timeout. That
        setting stays for the rest of the transaction, so a wait for a row held by another
        transaction is bounded too; use it only in a transaction the library owns. A no-op
        on SQLite, whose write transactions already exclude each other.
        """
        await self._ensure_protected(session)
        if session.dialect is Dialect.POSTGRES:
            await self._take_lock(session, self._lock_timeout)

    async def _take_lock(self, session: Session, wait: str) -> str:
        """Take this schema's append lock, waiting at most `wait`; return the old setting."""
        previous = str((await session.execute("SELECT current_setting('lock_timeout')"))[0][0])
        await session.execute("SELECT set_config('lock_timeout', ?, true)", (wait,))
        name = layout.audit_lock_name(self._table.schema or layout.DEFAULT_SCHEMA)
        try:
            await session.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (name,))
        except driver_errors() as error:
            if getattr(error, "sqlstate", None) == AuditLockTimeoutError.sqlstate:
                raise AuditLockTimeoutError(
                    f"The audit log's append lock was not free within {wait}; nothing was written."
                ) from error
            raise
        return previous

    async def _insert(
        self, session: Session, records: list[AuditRecord], database_now: datetime
    ) -> list[AuditRecord]:
        row_marks = "(" + ", ".join("?" for _ in COLUMNS.split(",")) + ")"
        if session.dialect is Dialect.SQLITE:
            # SQLite has no roles and no clock apart from the writer's: the library
            # writes recorded_at, and db_role stays empty.
            stored = [record.model_copy(update={"recorded_at": database_now}) for record in records]
            for record in stored:
                await session.execute(
                    f"INSERT INTO {self._table.sql} ({COLUMNS}) VALUES {row_marks}",
                    _row_values(record),
                )
            return stored
        # One statement for the whole batch. The insert trigger sets db_role and
        # recorded_at, whatever is sent, and checks the chain link of every row.
        rows = await session.execute(
            f"INSERT INTO {self._table.sql} ({COLUMNS}) VALUES "
            + ", ".join(row_marks for _ in records)
            + " RETURNING seq, db_role, recorded_at",
            [value for record in records for value in _row_values(record)],
        )
        written = {seq: (role, recorded) for seq, role, recorded in rows}
        return [
            record.model_copy(
                update={
                    "db_role": written[record.seq][0],
                    "recorded_at": datetime.fromisoformat(written[record.seq][1]),
                }
            )
            for record in records
        ]

    async def iter_records(self, *, after_seq: int = 0) -> AsyncIterator[AuditRecord]:
        """Yield records with seq greater than after_seq, in order, read in batches."""
        last_seq = after_seq
        while True:
            batch = partial(
                _rows_after, table=self._table, after_seq=last_seq, limit=READ_BATCH_SIZE
            )
            rows = await self.database.run(batch)
            for row in rows:
                record = record_from_row(row)
                last_seq = record.seq
                yield record
            if len(rows) < READ_BATCH_SIZE:
                return

    async def head(self) -> AuditHead:
        """The latest record's seq and hash; seq 0 and the genesis hash if the log is empty."""
        return await self.database.run(partial(_head_in, table=self._table))

    async def verify(self, *, expected_head: AuditHead | None = None) -> AuditHead:
        """Walk the whole chain, check every hash and seq, and return the head.

        The chain alone proves nothing: anyone who can rewrite rows can also rebuild
        every hash after them. Only a head kept somewhere else, passed here as
        expected_head, shows the log has not been rewritten or cut short since that
        head was taken. Raises AuditIntegrityError on any mismatch.
        """
        return await self._walk(expected_head)

    async def _walk(self, expected_head: AuditHead | None) -> AuditHead:
        """The walk itself, in batches, so a long log does not have to fit in memory."""
        head = AuditHead(seq=0, record_hash=GENESIS_HASH)
        anchored_hash = (
            GENESIS_HASH if expected_head is not None and expected_head.seq == 0 else None
        )
        while True:
            rows = await self.database.run(
                partial(_rows_after, table=self._table, after_seq=head.seq, limit=READ_BATCH_SIZE)
            )
            # Parsing and hashing a batch is CPU work, so it runs off the event loop.
            head, anchored_hash = await asyncio.to_thread(
                _check_batch, rows, head, expected_head, anchored_hash
            )
            if len(rows) < READ_BATCH_SIZE:
                break

        if expected_head is not None:
            _check_anchor(head, expected_head, anchored_hash)
        return head

    async def _ensure_protected(self, session: Session) -> None:
        # Checked once per log object; a role or trigger change later is not noticed.
        if self._protections_checked:
            return
        if session.dialect is Dialect.SQLITE:
            await _ensure_sqlite_schema(session)
        else:
            await layout.require_postgres_version(session)
            await _check_postgres_role(session, self._table)
            _require_triggers(
                session, await _postgres_triggers(session, self._table), POSTGRES_TRIGGERS
            )
            revision = await layout.audit_trigger_revision(session, self._table.sql)
            if revision != layout.AUDIT_TRIGGER_REVISION:
                raise ConfigError(
                    f"The audit insert trigger on {self._table} is older than this release "
                    f"(revision {revision or 'before 5'}, this release needs "
                    f"{layout.AUDIT_TRIGGER_REVISION}). As the owner role, run "
                    "install_postgres_schema from 0.1.0a5 with the requester and approver roles."
                )
        await bring_table_up_to_date(session, AUDIT_TABLE, ADDED_COLUMNS, schema=self._table.schema)
        self._protections_checked = True


def _check_batch(
    rows: list[tuple[Any, ...]],
    head: AuditHead,
    expected_head: AuditHead | None,
    anchored_hash: str | None,
) -> tuple[AuditHead, str | None]:
    """Check each row of a batch follows `head`; return the new head and the anchored hash."""
    for row in rows:
        record = record_from_row(row)
        _check_link(record, head)
        head = AuditHead(seq=record.seq, record_hash=record.record_hash)
        if expected_head is not None and record.seq == expected_head.seq:
            anchored_hash = record.record_hash
    return head, anchored_hash


def _check_link(record: AuditRecord, previous: AuditHead) -> None:
    """Raise AuditIntegrityError unless `record` follows `previous` and its hash holds."""
    if record.seq != previous.seq + 1:
        raise AuditIntegrityError(
            f"Record {previous.seq + 1} is missing: the next record is {record.seq}."
        )
    if record.prev_hash != previous.record_hash:
        raise AuditIntegrityError(f"Record {record.seq} does not link to record {previous.seq}.")
    try:
        hash_holds = compute_record_hash(record) == record.record_hash
    except (ValueError, RecursionError) as error:
        # Content the hash cannot be taken of, such as a number JSON cannot spell.
        raise AuditIntegrityError(f"Record {record.seq} cannot be hashed.") from error
    if not hash_holds:
        # A row the database accepted with a wrong hash shows who inserted it.
        by = (
            f" (its db_role column reads {record.db_role}; the hash does not cover it)"
            if record.db_role
            else ""
        )
        raise AuditIntegrityError(f"Record {record.seq} was altered after it was written{by}.")


async def audit_table_exists(database: Database, *, schema: str | None = None) -> bool:
    """True when the database holds an audit table, so a check of it means something."""
    return await database.run(
        partial(_table_is_readable, table=TableName.on(database, AUDIT_TABLE, schema))
    )


def _check_anchor(head: AuditHead, expected: AuditHead, anchored_hash: str | None) -> None:
    """Fail unless the walked log still contains the expected head at its seq."""
    if expected.seq > head.seq:
        raise AuditIntegrityError(
            f"The log ends at record {head.seq}, but the anchor was taken at record "
            f"{expected.seq}: records were removed from the end."
        )
    if anchored_hash != expected.record_hash:
        raise AuditIntegrityError(
            f"Record {expected.seq} no longer matches the anchor: the log was rewritten."
        )


async def _ensure_sqlite_schema(session: Session) -> None:
    """Create the table and triggers in a new database; refuse a table that lost them."""
    if not await _sqlite_table_exists(session):
        for statement in SQLITE_SCHEMA:
            await session.execute(statement)
        return
    triggers = {
        row[0]
        for row in await session.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (AUDIT_TABLE,),
        )
    }
    _require_triggers(session, triggers, SQLITE_TRIGGERS)
    await _upgrade_sqlite_append_trigger(session)


async def _upgrade_sqlite_append_trigger(session: Session) -> None:
    """Replace a 0.1.0a3 append trigger, which checked only seq, with the linking one."""
    rows = await session.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (APPEND_TRIGGER,)
    )
    if rows and "prev_hash" not in rows[0][0]:
        await session.execute(f"DROP TRIGGER {APPEND_TRIGGER}")
        await session.execute(SQLITE_SCHEMA[3])


def _require_triggers(session: Session, present: set[str], required: frozenset[str]) -> None:
    missing = sorted(required - present)
    if missing:
        raise AuditIntegrityError(
            f"The audit table's protecting triggers are missing ({', '.join(missing)}), so "
            "nothing more is written. Run verify to check the existing records, then "
            "restore the triggers deliberately."
        )


# The connecting role must not be able to change the audit table, directly or
# through any role it can SET ROLE to, whether or not it inherits that role's
# privileges. Every role that the current user or the session user (the one that
# logged in, which SET ROLE NONE returns to) is a member of is checked for
# superuser, ownership, and table- or column-level change rights.
_ROLE_CHECK_SQL = """
SELECT
    c.oid IS NOT NULL,
    EXISTS (
        SELECT 1 FROM pg_roles m
        WHERE (pg_has_role(current_user, m.oid, 'MEMBER')
               OR pg_has_role(session_user, m.oid, 'MEMBER'))
          AND (
              m.rolsuper
              OR m.oid = c.relowner
              OR has_table_privilege(m.oid, c.oid, 'UPDATE')
              OR has_table_privilege(m.oid, c.oid, 'DELETE')
              OR has_table_privilege(m.oid, c.oid, 'TRUNCATE')
              OR has_any_column_privilege(m.oid, c.oid, 'UPDATE')
          )
    )
FROM (SELECT to_regclass(?) AS oid) AS target
LEFT JOIN pg_class c ON c.oid = target.oid
"""


async def _check_postgres_role(session: Session, table: TableName) -> None:
    table_exists, can_change = (await session.execute(_ROLE_CHECK_SQL, (table.sql,)))[0]
    if not table_exists:
        raise ConfigError(
            f"Table {table} does not exist; install it with "
            "storage.install_postgres_schema as the owner role."
        )
    if can_change:
        raise ConfigError(
            "The audit log's database role may only INSERT and SELECT on "
            f"{table}; this role, or a role it can switch to, is a superuser, owns "
            "the table, or can update, delete or truncate it. Connect as the application "
            "role."
        )


async def _postgres_triggers(session: Session, table: TableName) -> set[str]:
    return {
        row[0]
        for row in await session.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = to_regclass(?) AND NOT tgisinternal AND tgenabled <> 'D'",
            (table.sql,),
        )
    }


async def _sqlite_table_exists(session: Session) -> bool:
    return bool(
        await session.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (AUDIT_TABLE,)
        )
    )


async def _table_is_readable(session: Session, table: TableName) -> bool:
    if session.dialect is Dialect.SQLITE:
        return await _sqlite_table_exists(session)
    return bool((await session.execute("SELECT to_regclass(?) IS NOT NULL", (table.sql,)))[0][0])


async def _head_in(session: Session, table: TableName) -> AuditHead:
    # A missing table means nothing was ever written, which is an empty log, not an error.
    if not await _table_is_readable(session, table):
        return AuditHead(seq=0, record_hash=GENESIS_HASH)
    rows = await session.execute(
        f"SELECT seq, record_hash FROM {table.sql} ORDER BY seq DESC LIMIT 1"
    )
    if not rows:
        return AuditHead(seq=0, record_hash=GENESIS_HASH)
    seq, record_hash = rows[0]
    return AuditHead(seq=seq, record_hash=record_hash)


_POSTGRES_NOW_TEXT = (
    "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
)


async def _head_and_now(session: Session, table: TableName) -> tuple[AuditHead, datetime]:
    """The chain head and the database's clock, in one round trip on Postgres.

    Called after the append lock is held, as a statement of its own: each
    statement in READ COMMITTED takes a fresh snapshot, which is what makes the
    head it reads the one the lock protects. On SQLite the clock is the writer's.
    """
    if session.dialect is Dialect.SQLITE:
        return await _head_in(session, table), datetime.now(UTC)
    rows = await session.execute(
        f"SELECT h.seq, h.record_hash, {_POSTGRES_NOW_TEXT} FROM (SELECT 1) x "
        f"LEFT JOIN LATERAL (SELECT seq, record_hash FROM {table.sql} "
        "ORDER BY seq DESC LIMIT 1) h ON TRUE"
    )
    seq, record_hash, now_text = rows[0]
    head = (
        AuditHead(seq=0, record_hash=GENESIS_HASH)
        if seq is None
        else AuditHead(seq=seq, record_hash=record_hash)
    )
    return head, datetime.fromisoformat(now_text)


def _check_occurred_at(occurred_at: datetime | None, database_now: datetime) -> None:
    """Refuse a caller-supplied time too far from the database's clock."""
    if occurred_at is None:
        return
    if occurred_at > database_now + OCCURRED_AT_MAX_FUTURE:
        raise AuditTimeRejectedError(
            f"occurred_at {canonical_timestamp(occurred_at)} is more than "
            f"{OCCURRED_AT_MAX_FUTURE} ahead of the database's clock."
        )
    if occurred_at < database_now - OCCURRED_AT_MAX_PAST:
        raise AuditTimeRejectedError(
            f"occurred_at {canonical_timestamp(occurred_at)} is more than "
            f"{OCCURRED_AT_MAX_PAST} behind the database's clock."
        )


async def _rows_after(
    session: Session, table: TableName, after_seq: int, limit: int | None
) -> list[tuple[Any, ...]]:
    # limit is formatted in, not bound, and int() keeps that safe.
    if not await _table_is_readable(session, table):
        return []
    await bring_table_up_to_date(session, AUDIT_TABLE, ADDED_COLUMNS, schema=table.schema)
    limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""
    return await session.execute(
        f"SELECT {COLUMNS} FROM {table.sql} WHERE seq > ? ORDER BY seq{limit_clause}",
        (after_seq,),
    )


def _row_values(record: AuditRecord) -> tuple[Any, ...]:
    return (
        record.seq,
        record.schema_version,
        str(record.event_id),
        canonical_timestamp(record.occurred_at),
        record.action,
        record.actor_id,
        record.subject_id,
        canonical_json(record.payload).decode("utf-8"),
        (
            canonical_json(record.run_context.as_json()).decode("utf-8")
            if record.run_context is not None
            else None
        ),
        record.prev_hash,
        record.record_hash,
        None,
        canonical_timestamp(record.recorded_at) if record.recorded_at is not None else None,
    )


def record_from_row(row: tuple[Any, ...]) -> AuditRecord:
    """Rebuild a record from a COLUMNS-ordered row; AuditIntegrityError if it is malformed."""
    seq, schema_version, event_id, occurred_at, action, actor_id, subject_id = row[:7]
    payload, run_context, prev_hash, record_hash, db_role, recorded_at = row[7:]
    try:
        return AuditRecord.model_validate(
            {
                "seq": seq,
                "schema_version": schema_version,
                "event_id": UUID(event_id),
                "occurred_at": datetime.fromisoformat(occurred_at),
                "action": action,
                "actor_id": actor_id,
                "subject_id": subject_id,
                "payload": json.loads(payload),
                "run_context": json.loads(run_context) if run_context is not None else None,
                "prev_hash": prev_hash,
                "record_hash": record_hash,
                "db_role": db_role,
                "recorded_at": (
                    datetime.fromisoformat(recorded_at) if recorded_at is not None else None
                ),
            },
            context={STORED_RECORD: True},
        )
    except (ValueError, TypeError, ValidationError, RecursionError) as error:
        raise AuditIntegrityError(f"Record {seq} is malformed and cannot be checked.") from error
