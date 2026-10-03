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
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
    AuditEvent,
    AuditHead,
    AuditRecord,
    UnsealedAuditRecord,
)
from aox_agent_core.errors import AuditIntegrityError, AuditPayloadRejectedError, ConfigError
from aox_agent_core.replay.scrub import PatternScrubber, Scrubber
from aox_agent_core.storage import (
    Database,
    Dialect,
    Session,
    TableName,
    bring_table_up_to_date,
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

# pg_advisory_xact_lock key that serializes appends: ASCII "agentcor" as an int64.
APPEND_LOCK_KEY: Final = 0x6167656E74636F72

READ_BATCH_SIZE = 500

COLUMNS = (
    "seq, schema_version, event_id, occurred_at, action, actor_id, subject_id, payload, "
    "run_context, prev_hash, record_hash, db_role"
)
# Columns added in 0.1.0a3, with their SQLite types.
ADDED_IN_A3: Final = {"db_role": "TEXT"}

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
    db_role TEXT
)"""

SQLITE_SCHEMA: Final = (
    _TABLE_DDL,
    f"""CREATE TRIGGER {UPDATE_TRIGGER} BEFORE UPDATE ON {AUDIT_TABLE}
    BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END""",
    f"""CREATE TRIGGER {DELETE_TRIGGER} BEFORE DELETE ON {AUDIT_TABLE}
    BEGIN SELECT RAISE(ABORT, '{AUDIT_TABLE} is append-only'); END""",
    # Inserts land only right after the last record, with an event_id not seen
    # before. Besides keeping the sequence gapless, this stops INSERT OR REPLACE,
    # which deletes the row it conflicts with (on seq or event_id) without firing
    # the delete trigger.
    f"""CREATE TRIGGER {APPEND_TRIGGER} BEFORE INSERT ON {AUDIT_TABLE}
    WHEN NEW.seq <> (SELECT COALESCE(MAX(seq), 0) FROM {AUDIT_TABLE}) + 1
        OR EXISTS (SELECT 1 FROM {AUDIT_TABLE} WHERE event_id = NEW.event_id)
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
    """

    def __init__(
        self,
        database: Database,
        *,
        scrubber: Scrubber | None = None,
        schema: str | None = None,
    ) -> None:
        self.database = database
        self._scrubber = scrubber if scrubber is not None else PatternScrubber()
        self._table = TableName.on(database, AUDIT_TABLE, schema)
        self._protections_checked = False

    async def append(self, event: AuditEvent) -> AuditRecord:
        """Validate and scrub the event, then add it to the chain in its own transaction."""
        checked = self.checked_event(event)
        return await self.database.run(lambda session: self.append_in(session, checked), write=True)

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

    def append_in(self, session: Session, event: AuditEvent) -> AuditRecord:
        """Append within a write transaction on this log's database.

        For callers that must write their own change and its audit event together.
        The event must already have passed checked_event().
        """
        self._ensure_protected(session)
        if session.dialect is Dialect.POSTGRES:
            session.execute("SELECT pg_advisory_xact_lock(?)", (APPEND_LOCK_KEY,))
        head = _head_in(session, self._table)
        unsealed = UnsealedAuditRecord(
            seq=head.seq + 1,
            event_id=uuid4(),
            occurred_at=datetime.now(UTC),
            action=event.action,
            actor_id=event.actor_id,
            subject_id=event.subject_id,
            payload=event.payload,
            run_context=event.context,
            prev_hash=head.record_hash,
        )
        record = AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))
        # On Postgres the insert trigger sets db_role to the inserting role, whatever
        # is sent; SQLite has no roles and leaves it empty.
        rows = session.execute(
            f"INSERT INTO {self._table.sql} ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            + (" RETURNING db_role" if session.dialect is Dialect.POSTGRES else ""),
            _row_values(record),
        )
        return record.model_copy(update={"db_role": rows[0][0]}) if rows else record

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
        return await asyncio.to_thread(self._verify_sync, expected_head)

    def _verify_sync(self, expected_head: AuditHead | None) -> AuditHead:
        """The walk itself, on a worker thread and in batches, so a long log neither
        blocks the event loop nor has to fit in memory."""
        head = AuditHead(seq=0, record_hash=GENESIS_HASH)
        anchored_hash = (
            GENESIS_HASH if expected_head is not None and expected_head.seq == 0 else None
        )
        while True:
            rows = self.database.run_sync(
                partial(_rows_after, table=self._table, after_seq=head.seq, limit=READ_BATCH_SIZE)
            )
            for row in rows:
                record = record_from_row(row)
                _check_link(record, head)
                head = AuditHead(seq=record.seq, record_hash=record.record_hash)
                if expected_head is not None and record.seq == expected_head.seq:
                    anchored_hash = record.record_hash
            if len(rows) < READ_BATCH_SIZE:
                break

        if expected_head is not None:
            _check_anchor(head, expected_head, anchored_hash)
        return head

    def _ensure_protected(self, session: Session) -> None:
        # Checked once per log object; a role or trigger change later is not noticed.
        if self._protections_checked:
            return
        if session.dialect is Dialect.SQLITE:
            _ensure_sqlite_schema(session)
        else:
            _check_postgres_role(session, self._table)
            _require_triggers(session, _postgres_triggers(session, self._table), POSTGRES_TRIGGERS)
        bring_table_up_to_date(session, AUDIT_TABLE, ADDED_IN_A3, schema=self._table.schema)
        self._protections_checked = True


def _check_link(record: AuditRecord, previous: AuditHead) -> None:
    """Raise AuditIntegrityError unless `record` follows `previous` and its hash holds."""
    if record.seq != previous.seq + 1:
        raise AuditIntegrityError(
            f"Record {previous.seq + 1} is missing: the next record is {record.seq}."
        )
    if record.prev_hash != previous.record_hash:
        raise AuditIntegrityError(f"Record {record.seq} does not link to record {previous.seq}.")
    if compute_record_hash(record) != record.record_hash:
        raise AuditIntegrityError(f"Record {record.seq} was altered after it was written.")


def audit_table_exists(database: Database, *, schema: str | None = None) -> bool:
    """True when the database holds an audit table, so a check of it means something."""
    return database.run_sync(
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


def _ensure_sqlite_schema(session: Session) -> None:
    """Create the table and triggers in a new database; refuse a table that lost them."""
    if not _sqlite_table_exists(session):
        for statement in SQLITE_SCHEMA:
            session.execute(statement)
        return
    triggers = {
        row[0]
        for row in session.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (AUDIT_TABLE,),
        )
    }
    _require_triggers(session, triggers, SQLITE_TRIGGERS)


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


def _check_postgres_role(session: Session, table: TableName) -> None:
    table_exists, can_change = session.execute(_ROLE_CHECK_SQL, (table.sql,))[0]
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


def _postgres_triggers(session: Session, table: TableName) -> set[str]:
    return {
        row[0]
        for row in session.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = to_regclass(?) AND NOT tgisinternal AND tgenabled <> 'D'",
            (table.sql,),
        )
    }


def _sqlite_table_exists(session: Session) -> bool:
    return bool(
        session.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (AUDIT_TABLE,)
        )
    )


def _table_is_readable(session: Session, table: TableName) -> bool:
    if session.dialect is Dialect.SQLITE:
        return _sqlite_table_exists(session)
    return bool(session.execute("SELECT to_regclass(?) IS NOT NULL", (table.sql,))[0][0])


def _head_in(session: Session, table: TableName) -> AuditHead:
    # A missing table means nothing was ever written, which is an empty log, not an error.
    if not _table_is_readable(session, table):
        return AuditHead(seq=0, record_hash=GENESIS_HASH)
    rows = session.execute(f"SELECT seq, record_hash FROM {table.sql} ORDER BY seq DESC LIMIT 1")
    if not rows:
        return AuditHead(seq=0, record_hash=GENESIS_HASH)
    seq, record_hash = rows[0]
    return AuditHead(seq=seq, record_hash=record_hash)


def _rows_after(
    session: Session, table: TableName, after_seq: int, limit: int | None
) -> list[tuple[Any, ...]]:
    # limit is formatted in, not bound, and int() keeps that safe.
    if not _table_is_readable(session, table):
        return []
    bring_table_up_to_date(session, AUDIT_TABLE, ADDED_IN_A3, schema=table.schema)
    limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""
    return session.execute(
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
    )


def record_from_row(row: tuple[Any, ...]) -> AuditRecord:
    """Rebuild a record from a COLUMNS-ordered row; AuditIntegrityError if it is malformed."""
    seq, schema_version, event_id, occurred_at, action, actor_id, subject_id = row[:7]
    payload, run_context, prev_hash, record_hash, db_role = row[7:]
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
            },
            context={STORED_RECORD: True},
        )
    except (ValueError, TypeError, ValidationError) as error:
        raise AuditIntegrityError(f"Record {seq} is malformed and cannot be checked.") from error
