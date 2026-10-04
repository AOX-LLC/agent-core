"""The Postgres schema for the audit log and the approval queue: install and checks.

Three roles share it. The owner creates everything and is never used at run
time. The requester role (the agent side) submits requests, consumes approved
ones and cancels its own pending ones. The approver role (the decision side)
approves or rejects pending ones. Both append to the audit log and may store a
request's expiry once it is due.

Grants are the first layer. The real enforcement is a guard trigger on the
approvals table that checks every insert and update against the transitions
below, using the database's own clock and role membership, so a role holding
only its own credentials cannot step outside them with plain SQL:

    from      to         role                    also required
    (insert)  pending    requester               undecided; starts by now, lives at most 168 hours
    pending   approved   approver                decision 'approve', resolved_by set and neither
                                                 requester nor delegate, resolved_at set (not before
                                                 created_at, within 5 minutes of now), not expired
    pending   rejected   approver                as approved, with decision 'reject'
    pending   cancelled  requester               closed_at set
    pending   expired    requester or approver   closed_at set, expires_at already past
    approved  expired    requester or approver   as above: an approval that lapsed unused
    approved  consumed   requester               consumed_at set, not expired

One more change is allowed, to the stored payload alone: the approver role may purge
it (payload_json to NULL, payload_purged_at set) on a finished request (consumed,
rejected, cancelled or expired) whose finish time is further back than the installed
retention floor by the database's clock. The guard writes closed_at, consumed_at, a
rejection's resolved_at and payload_purged_at itself, from its own clock, whatever the
statement carried, so a finish time cannot be backdated.
Every other change is refused, as are DELETE and TRUNCATE, and no update may touch a
request's identity, payload hash, requester, required role, lifetime, run context or
delegates.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from aox_agent_core.errors import ConfigError

if TYPE_CHECKING:
    from aox_agent_core.storage import Session

AUDIT_TABLE: Final = "agent_core_audit"
# The advisory lock that serializes audit appends is taken on
# hashtextextended('agent_core_audit:' || <schema>, 0): one lock per schema, so two installs
# in one database do not wait for each other. The library takes it before reading the chain
# head, and the insert trigger takes it too, so even a plain INSERT is serialized and sees
# the committed head. Both compute the key in SQL, so they cannot disagree.
AUDIT_LOCK_PREFIX: Final = "agent_core_audit:"
# Which revision of the guard function and the audit insert trigger this release writes
# (a comment inside each). A connection refuses an older one, so a release that changes
# them is not run against a schema it has not been installed over.
GUARD_REVISION: Final = 7
AUDIT_TRIGGER_REVISION: Final = 6
# The oldest Postgres the library is tested on and supports (16.0).
POSTGRES_MINIMUM_VERSION_NUM: Final = 160000
# Most bytes of canonical JSON stored as an approval's payload.
MAX_STORED_PAYLOAD_BYTES: Final = 8192
APPROVALS_TABLE: Final = "agent_core_approvals"
# One open request per requester, action and payload hash: pending and approved-unused
# rows. A partial unique index, so it holds when submits race.
OPEN_REQUEST_INDEX: Final = "agent_core_approvals_one_open"
OPEN_REQUEST_COLUMNS: Final = ("requested_by", "action", "payload_sha256")
OPEN_REQUEST_STATUSES: Final = ("pending", "approved")
# What purge_payloads scans for: finished requests that still hold a payload, by finish time.
# On Postgres the library's scan also requires the finish time to be a canonical timestamp
# (a planted row with another spelling is left alone), and the index carries that condition:
# a partial index serves a query only if the query's WHERE implies the index predicate, and
# with the condition in the query alone the planner read every due row and sorted them.
# The index was renamed in 0.1.0a6 because 0.1.0a5's has the narrower predicate.
PURGEABLE_INDEX: Final = "agent_core_approvals_purge_due"
LEGACY_PURGEABLE_INDEX: Final = "agent_core_approvals_purgeable"
FINISHED_AT_EXPRESSION: Final = (
    "CASE status WHEN 'consumed' THEN consumed_at WHEN 'rejected' THEN resolved_at "
    "ELSE closed_at END"
)
CANONICAL_STAMP_PATTERN: Final = r"'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}[.][0-9]{6}Z$'"
PURGEABLE_PREDICATE: Final = (
    "status IN ('consumed', 'rejected', 'cancelled', 'expired') "
    "AND payload_json IS NOT NULL AND payload_purged_at IS NULL"
)
PURGEABLE_PREDICATE_POSTGRES: Final = (
    f"{PURGEABLE_PREDICATE} AND ({FINISHED_AT_EXPRESSION}) ~ {CANONICAL_STAMP_PATTERN}"
)
ROLES_TABLE: Final = "agent_core_approval_roles"
# Login binding (0.1.0a7): an owner-managed table maps each approver login (session_user) to
# the one principal id it may record as resolved_by. Rows are never deleted, so a login or a
# principal is never reused; a mapping ends by setting removed_at.
LOGINS_TABLE: Final = "agent_core_approver_logins"
LOGINS_TRIGGER: Final = "agent_core_approver_logins_guard"
LOGINS_TRUNCATE_TRIGGER: Final = "agent_core_approver_logins_no_truncate"
# Revision 2 (0.1.0): the guard refuses an INSERT or UPDATE unless session_user and
# current_user are both the table's owner, so a role that reaches the table without a grant
# (a member of pg_write_all_data) cannot map a login. Revision 1 shipped in 0.1.0a7.
LOGINS_GUARD_REVISION: Final = 2
LOGINS_GUARD_MARKER: Final = "-- agent-core login mapping guard revision"
LOGINS_TRIGGERS: Final = frozenset({LOGINS_TRIGGER, LOGINS_TRUNCATE_TRIGGER})
BOUND_PRINCIPAL_FUNCTION: Final = "agent_core_bound_principal"
BINDING_MARKER: Final = "-- agent-core login binding"

AUDIT_UPDATE_DELETE_TRIGGER: Final = "agent_core_audit_no_update_delete"
AUDIT_TRUNCATE_TRIGGER: Final = "agent_core_audit_no_truncate"
AUDIT_APPEND_TRIGGER: Final = "agent_core_audit_append_at_end"
APPROVALS_GUARD_TRIGGER: Final = "agent_core_approvals_guard"
APPROVALS_TRUNCATE_TRIGGER: Final = "agent_core_approvals_no_truncate"
AUDIT_TRIGGERS: Final = frozenset(
    {AUDIT_UPDATE_DELETE_TRIGGER, AUDIT_TRUNCATE_TRIGGER, AUDIT_APPEND_TRIGGER}
)
APPROVALS_TRIGGERS: Final = frozenset({APPROVALS_GUARD_TRIGGER, APPROVALS_TRUNCATE_TRIGGER})

DEFAULT_SCHEMA: Final = "public"
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# pg_advisory_xact_lock key that serializes installs: ASCII "acinstal" as an int64.
INSTALL_LOCK_KEY: Final = 0x6163696E7374616C

# What each run-time role is granted. None stands for the whole table; a set of
# names, for those columns only.
REQUESTER_LAYOUT: Final[Mapping[str, Mapping[str, frozenset[str] | None]]] = {
    AUDIT_TABLE: {"SELECT": None, "INSERT": None},
    APPROVALS_TABLE: {
        "SELECT": None,
        "INSERT": None,
        "UPDATE": frozenset({"status", "consumed_at", "closed_at"}),
    },
}
APPROVER_LAYOUT: Final[Mapping[str, Mapping[str, frozenset[str] | None]]] = {
    AUDIT_TABLE: {"SELECT": None, "INSERT": None},
    APPROVALS_TABLE: {
        "SELECT": None,
        "UPDATE": frozenset(
            {
                "status",
                "decision",
                "resolved_by",
                "resolved_at",
                "reason",
                "closed_at",
                "payload_json",
                "payload_purged_at",
            }
        ),
    },
}
# Which role is which is no secret (pg_roles lists every role), and every role
# must be able to read it, so the connect check can say what is wrong.
PUBLIC_LAYOUT: Final[Mapping[str, Mapping[str, frozenset[str] | None]]] = {
    ROLES_TABLE: {"SELECT": None},
}
# Decision columns the requester role must never be able to write.
DECISION_COLUMNS: Final = ("decision", "resolved_by", "resolved_at", "reason")
# Columns only the approver role may change: a stored payload is purged by the decision side.
PURGE_COLUMNS: Final = ("payload_json", "payload_purged_at")
# Everything the requester role must never be able to write on an approval request.
REQUESTER_FORBIDDEN_COLUMNS: Final = (*DECISION_COLUMNS, *PURGE_COLUMNS)
# The shortest retention the installed guard allows a purge, unless the installer is told otherwise.
DEFAULT_PAYLOAD_RETENTION_FLOOR_SECONDS: Final = 24 * 60 * 60


# Characters of the Unicode categories Cc, Cf, Zl and Zp that free text may not carry; a
# subset of what the library refuses (tests check it), so the database never refuses
# what the library accepts. Backslashes are for Postgres' regex, not for Python.
UNSAFE_TEXT_PATTERN: Final = (
    r"[\u0001-\u001f\u007f-\u009f\u00ad\u0600-\u0605\u061c\u06dd\u070f\u08e2\u180e"
    r"\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]"
)


def audit_lock_name(schema: str) -> str:
    """The text the audit append lock of `schema` is keyed on."""
    return f"{AUDIT_LOCK_PREFIX}{schema}"


def identifier(name: str, *, what: str) -> str:
    """`name` double-quoted, after checking it is a plain lowercase identifier."""
    if not IDENTIFIER.fullmatch(name):
        raise ConfigError(f"{name!r} is not a plain Postgres {what} name.")
    return f'"{name}"'


def audit_ddl(schema: str) -> tuple[str, ...]:
    """The audit table, its column additions since 0.1.0a1, its triggers and PUBLIC revoke."""
    table = f"{identifier(schema, what='schema')}.{AUDIT_TABLE}"
    functions = identifier(schema, what="schema")
    return (
        f"""CREATE TABLE IF NOT EXISTS {table} (
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
            recorded_at TEXT,
            db_login TEXT
        )""",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS db_role TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS db_login TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS recorded_at TEXT",
        # Both functions pin search_path and reach the table through the trigger's
        # own schema and name, so a temporary table cannot stand in for it.
        f"""CREATE OR REPLACE FUNCTION {functions}.{AUDIT_TABLE}_refuse_change()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN RAISE EXCEPTION '{AUDIT_TABLE} is append-only'; END $$""",
        f"""CREATE OR REPLACE TRIGGER {AUDIT_UPDATE_DELETE_TRIGGER}
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {functions}.{AUDIT_TABLE}_refuse_change()""",
        f"""CREATE OR REPLACE TRIGGER {AUDIT_TRUNCATE_TRIGGER} BEFORE TRUNCATE ON {table}
        FOR EACH STATEMENT EXECUTE FUNCTION {functions}.{AUDIT_TABLE}_refuse_change()""",
        # The insert trigger enforces the chain's linkage (the next seq, prev_hash equal
        # to the head's record_hash), the shape of every field it can check, and the
        # bounds on occurred_at; it sets db_role (current_user), db_login (session_user, which
        # SET ROLE does not change) and recorded_at from the database's own idea of who is
        # inserting and when, whatever the insert supplied. It cannot
        # check record_hash itself, which verify() recomputes.
        f"""CREATE OR REPLACE FUNCTION {functions}.{AUDIT_TABLE}_append_at_end()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        {_AUDIT_APPEND_BODY}
        $$""",
        f"""CREATE OR REPLACE TRIGGER {AUDIT_APPEND_TRIGGER} BEFORE INSERT ON {table}
        FOR EACH ROW EXECUTE FUNCTION {functions}.{AUDIT_TABLE}_append_at_end()""",
        f"REVOKE ALL ON {table} FROM PUBLIC",
    )


_AUDIT_APPEND_BODY = (
    """
-- agent-core audit trigger revision <audit_revision>
DECLARE
    head_seq bigint;
    head_hash text;
    db_now timestamptz := clock_timestamp();
    stamp text := 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"';
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('<lock_prefix>' || TG_TABLE_SCHEMA, 0));
    EXECUTE format('SELECT seq, record_hash FROM %I.%I ORDER BY seq DESC LIMIT 1',
                   TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO head_seq, head_hash;
    IF head_seq IS NULL THEN
        head_seq := 0;
        head_hash := repeat('0', 64);
    END IF;
    IF NEW.seq <> head_seq + 1 OR NEW.prev_hash IS DISTINCT FROM head_hash THEN
        RAISE EXCEPTION '<table> is append-only: a record must follow the last one';
    END IF;
    IF NEW.action !~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*)*$' OR length(NEW.action) > 100
       OR NEW.actor_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
       OR (NEW.subject_id IS NOT NULL
           AND NEW.subject_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')
       OR octet_length(NEW.payload) > 8192
       OR jsonb_typeof(NEW.payload::jsonb) IS DISTINCT FROM 'object'
       OR (NEW.run_context IS NOT NULL
           AND (octet_length(NEW.run_context) > 2048
                OR jsonb_typeof(NEW.run_context::jsonb) IS DISTINCT FROM 'object')) THEN
        RAISE EXCEPTION '<table> is append-only: a new record has a field of the wrong shape';
    END IF;
    IF NEW.schema_version IS DISTINCT FROM 4
       OR NEW.event_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NEW.record_hash !~ '^[0-9a-f]{64}$'
       OR NEW.occurred_at !~
          '^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9][.][0-9]{6}Z$'
       OR to_char((NEW.occurred_at)::timestamptz AT TIME ZONE 'UTC', stamp) <> NEW.occurred_at THEN
        RAISE EXCEPTION '<table> is append-only: a new record has a field of the wrong shape';
    END IF;
    IF NEW.occurred_at::timestamptz > db_now + interval '5 minutes'
       OR NEW.occurred_at::timestamptz < db_now - interval '24 hours' THEN
        RAISE EXCEPTION '<table>: occurred_at is too far from the database clock';
    END IF;
    NEW.recorded_at := to_char(db_now AT TIME ZONE 'UTC', stamp);
    NEW.db_role := current_user;
    NEW.db_login := session_user;
    RETURN NEW;
END
""".replace("<lock_prefix>", AUDIT_LOCK_PREFIX)
    .replace("<table>", AUDIT_TABLE)
    .replace("<audit_revision>", str(AUDIT_TRIGGER_REVISION))
)


def approvals_tables_ddl(schema: str) -> tuple[str, ...]:
    """The approvals and roles tables and the columns added since 0.1.0a2."""
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{APPROVALS_TABLE}"
    roles = f"{quoted_schema}.{ROLES_TABLE}"
    return (
        f"""CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            required_role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decision TEXT,
            resolved_by TEXT,
            resolved_at TEXT,
            consumed_at TEXT,
            reason TEXT,
            run_context TEXT,
            closed_at TEXT,
            delegates TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT,
            payload_purged_at TEXT
        )""",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS closed_at TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payload_json TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payload_purged_at TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS delegates TEXT NOT NULL DEFAULT '[]'",
        f"""CREATE INDEX IF NOT EXISTS agent_core_approvals_pending
        ON {table} (status, created_at, id)""",
        f"DROP INDEX IF EXISTS {quoted_schema}.{LEGACY_PURGEABLE_INDEX}",
        f"""CREATE INDEX IF NOT EXISTS {PURGEABLE_INDEX}
        ON {table} (({FINISHED_AT_EXPRESSION}), id) WHERE {PURGEABLE_PREDICATE_POSTGRES}""",
        f"""CREATE TABLE IF NOT EXISTS {roles} (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            requester_role TEXT NOT NULL,
            approver_role TEXT NOT NULL
        )""",
        f"REVOKE ALL ON {table} FROM PUBLIC",
    )


def logins_ddl(schema: str, requester_role: str, approver_role: str) -> tuple[str, ...]:
    """The login mapping table, its guard, the lookup function and their grants.

    Always installed, so an owner can map logins before turning binding on. Nothing on the
    table is granted to the requester or approver role: the guard reads it through
    agent_core_bound_principal(), which runs as the owner (SECURITY DEFINER) and returns
    only the connecting login's own principal. session_user is unchanged inside it.
    """
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{LOGINS_TABLE}"
    function = f"{quoted_schema}.{BOUND_PRINCIPAL_FUNCTION}"
    approver = identifier(approver_role, what="role")
    return (
        f"""CREATE TABLE IF NOT EXISTS {table} (
            login TEXT PRIMARY KEY CHECK (login <> ''),
            -- The role's OID at the time it was mapped: a role dropped and created again
            -- under the same name has a new OID and inherits nothing.
            login_oid OID NOT NULL UNIQUE,
            principal TEXT NOT NULL UNIQUE
                CHECK (principal ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'),
            mapped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            removed_at TIMESTAMPTZ
        )""",
        f"""CREATE OR REPLACE FUNCTION {quoted_schema}.{LOGINS_TABLE}_guard()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        {LOGINS_GUARD_MARKER} {LOGINS_GUARD_REVISION}
        BEGIN
            IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                RAISE EXCEPTION 'login mappings are never deleted; set removed_at to end one';
            END IF;
            -- Privileges do not decide this: a member of pg_write_all_data passes every
            -- grant check. Only the owner, connected as itself, writes the table. This
            -- function is not SECURITY DEFINER, so both names are the caller's.
            -- Roles are matched by exact name in pg_roles. A ::regrole cast parses its text
            -- as an identifier and folds case, so a login named "OWNER" would pass as owner.
            IF (SELECT relowner FROM pg_class WHERE oid = TG_RELID)
               IS DISTINCT FROM (SELECT oid FROM pg_roles WHERE rolname = session_user)
               OR (SELECT relowner FROM pg_class WHERE oid = TG_RELID)
               IS DISTINCT FROM (SELECT oid FROM pg_roles WHERE rolname = current_user) THEN
                RAISE EXCEPTION
                    'login mappings are written only by the table owner, connected as itself';
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.mapped_at := now();
                NEW.removed_at := NULL;
                RETURN NEW;
            END IF;
            IF OLD.removed_at IS NOT NULL OR NEW.removed_at IS NULL
               OR ROW(NEW.login, NEW.login_oid, NEW.principal, NEW.mapped_at)
                  IS DISTINCT FROM
                  ROW(OLD.login, OLD.login_oid, OLD.principal, OLD.mapped_at) THEN
                RAISE EXCEPTION 'a login mapping changes only by being removed, once';
            END IF;
            NEW.removed_at := now();
            RETURN NEW;
        END $$""",
        f"""CREATE OR REPLACE TRIGGER {LOGINS_TRIGGER}
        BEFORE INSERT OR UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {quoted_schema}.{LOGINS_TABLE}_guard()""",
        f"""CREATE OR REPLACE TRIGGER {LOGINS_TRUNCATE_TRIGGER} BEFORE TRUNCATE ON {table}
        FOR EACH STATEMENT EXECUTE FUNCTION {quoted_schema}.{LOGINS_TABLE}_guard()""",
        f"REVOKE ALL ON {table} FROM PUBLIC",
        # Table-level REVOKE also removes column grants the owner made, not another role's.
        f"REVOKE ALL ON {table} FROM {identifier(requester_role, what='role')}, {approver}",
        f"""CREATE OR REPLACE FUNCTION {function}() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT m.principal FROM {table} m JOIN pg_roles r ON r.oid = m.login_oid
            WHERE r.rolname = session_user::text AND m.login = r.rolname AND m.removed_at IS NULL
        $$""",
        f"REVOKE ALL ON FUNCTION {function}() FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {function}() TO {approver}",
    )


def open_request_index_ddl(schema: str) -> str:
    """The unique index that allows one open request per requester, action and payload hash."""
    table = f"{identifier(schema, what='schema')}.{APPROVALS_TABLE}"
    statuses = ", ".join(f"'{status}'" for status in OPEN_REQUEST_STATUSES)
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {OPEN_REQUEST_INDEX} ON {table} "
        f"({', '.join(OPEN_REQUEST_COLUMNS)}) WHERE status IN ({statuses})"
    )


def approvals_guard_ddl(
    schema: str,
    requester_role: str,
    approver_role: str,
    payload_retention_floor_seconds: int = DEFAULT_PAYLOAD_RETENTION_FLOOR_SECONDS,
    *,
    bind_logins: bool = False,
) -> tuple[str, ...]:
    """The guard function and its triggers.

    The role names are written into the guard itself, so it never depends on what
    the role it is checking may read.
    """
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{APPROVALS_TABLE}"
    # identifier() admits only [a-z0-9_], so the names are safe inside quotes.
    identifier(requester_role, what="role")
    identifier(approver_role, what="role")
    if payload_retention_floor_seconds < 0:
        raise ConfigError("The payload retention floor cannot be negative.")
    guard_body = _with_timestamp_checks(
        _GUARD_BODY.replace("'<requester>'", f"'{requester_role}'")
        .replace("'<approver>'", f"'{approver_role}'")
        .replace("<max_payload>", str(MAX_STORED_PAYLOAD_BYTES))
        .replace("<unsafe_text>", UNSAFE_TEXT_PATTERN)
        .replace("<revision>", str(GUARD_REVISION))
        .replace("<retention_floor>", str(int(payload_retention_floor_seconds)))
        .replace("<binding>", "on" if bind_logins else "off")
        .replace("<binding_check>", _binding_check(quoted_schema) if bind_logins else "")
    )
    return (
        f"""CREATE OR REPLACE FUNCTION {quoted_schema}.{APPROVALS_TABLE}_guard()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        {guard_body}
        $$""",
        f"""CREATE OR REPLACE TRIGGER {APPROVALS_GUARD_TRIGGER}
        BEFORE INSERT OR UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {quoted_schema}.{APPROVALS_TABLE}_guard()""",
        f"""CREATE OR REPLACE TRIGGER {APPROVALS_TRUNCATE_TRIGGER} BEFORE TRUNCATE ON {table}
        FOR EACH STATEMENT EXECUTE FUNCTION {quoted_schema}.{APPROVALS_TABLE}_guard()""",
    )


def _binding_check(quoted_schema: str) -> str:
    """The guard's login-binding rule. Written into the guard only when binding is on:
    Postgres checks EXECUTE on a function when it prepares the statement that calls it, so a
    call left in the guard while binding is off would still refuse a role without EXECUTE."""
    return (
        f"        IF NEW.resolved_by IS DISTINCT FROM {quoted_schema}.{BOUND_PRINCIPAL_FUNCTION}()"
        " THEN\n"
        "            RAISE EXCEPTION 'resolved_by must be the principal mapped to the deciding "
        "login';\n"
        "        END IF;\n"
    )


def _with_timestamp_checks(body: str) -> str:
    """Expand NOT_CANONICAL(x): true unless x is exactly a timestamp the library writes.

    The pattern alone admits values Postgres normalizes (24:00, a leap second,
    30 February cast to a later day) that the library cannot read back, so the
    value must also survive a round trip through timestamptz unchanged. A value
    that does not cast at all raises, which refuses the statement too.
    """
    return re.sub(
        r"NOT_CANONICAL\(([A-Za-z_.]+)\)",
        lambda match: (
            f"({match[1]} !~ timestamp_shape OR to_char(({match[1]})::timestamptz "
            f"AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') <> {match[1]})"
        ),
        body,
    )


# The guard carries the installed role names as literals. A role counts as the
# requester only if neither current_user nor session_user can act as the
# approver, and the other way round, so SET ROLE cannot cross sides. A superuser
# is a member of every role, so it is neither, and is refused.
_GUARD_BODY = """
-- agent-core guard revision <revision>
-- agent-core payload retention floor <retention_floor> seconds
-- agent-core login binding <binding>
-- With binding on, a decision's resolved_by must be the principal the owner mapped to
-- session_user (the login that authenticated, which SET ROLE does not change). The marker
-- line above is what the library reads; nothing else in this function switches it.
DECLARE
    requester_role text := '<requester>';
    approver_role text := '<approver>';
    -- The shapes the library writes; rows of any other shape are refused, so
    -- casts never depend on session settings and every row parses on read.
    timestamp_shape text :=
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9][.][0-9]{6}Z$';
    principal_shape text := '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$';
    unsafe_text text := '<unsafe_text>';
    opaque_shape text := '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$';
    run_context jsonb;
    is_overlong boolean;
    as_requester boolean;
    as_approver boolean;
    db_now timestamptz := statement_timestamp();
    is_expired boolean;
    is_purge boolean;
    finished_at text;
    -- When a request is closed, rejected, used or purged, the database writes the time
    -- itself: a client cannot backdate it to slip under the payload retention floor, or
    -- future-date it, and a purge run longer than the 5 minute skew bound still passes.
    db_stamp text := to_char(statement_timestamp() AT TIME ZONE 'UTC',
                             'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'approval requests are never deleted';
    END IF;
    as_requester := pg_has_role(current_user, requester_role, 'MEMBER')
        AND NOT pg_has_role(current_user, approver_role, 'MEMBER')
        AND NOT pg_has_role(session_user, approver_role, 'MEMBER');
    as_approver := pg_has_role(current_user, approver_role, 'MEMBER')
        AND NOT pg_has_role(current_user, requester_role, 'MEMBER')
        AND NOT pg_has_role(session_user, requester_role, 'MEMBER');

    IF TG_OP = 'INSERT' THEN
        IF NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may submit approval requests';
        END IF;
        IF NEW.id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR NEW.action !~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*)*$'
           OR length(NEW.action) > 100
           OR length(NEW.summary) NOT BETWEEN 1 AND 500
           OR NEW.summary ~ unsafe_text
           OR NEW.payload_sha256 !~ '^[0-9a-f]{64}$'
           OR NEW.requested_by !~ principal_shape
           OR NEW.required_role !~ '^[a-z][a-z0-9_.-]{0,63}$'
           OR NOT_CANONICAL(NEW.created_at)
           OR NOT_CANONICAL(NEW.expires_at) THEN
            RAISE EXCEPTION 'a new approval request has a field of the wrong shape';
        END IF;
        IF octet_length(NEW.delegates) > 4096
           OR jsonb_typeof(NEW.delegates::jsonb) <> 'array'
           OR jsonb_array_length(NEW.delegates::jsonb) > 16
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.delegates::jsonb) AS delegate
               WHERE jsonb_typeof(delegate) <> 'string' OR delegate #>> '{}' !~ principal_shape
           ) THEN
            RAISE EXCEPTION 'delegates must be at most 16 principal ids';
        END IF;
        IF NEW.run_context IS NOT NULL THEN
            IF octet_length(NEW.run_context) > 2048 THEN
                RAISE EXCEPTION 'a run context is at most 2048 bytes';
            END IF;
            run_context := NEW.run_context::jsonb;
            IF jsonb_typeof(run_context) <> 'object'
               OR EXISTS (
                   SELECT 1 FROM jsonb_object_keys(run_context) AS key
                   WHERE key NOT IN ('run_id', 'external_ids')
               )
               OR jsonb_typeof(run_context -> 'run_id') IS DISTINCT FROM 'string'
               OR run_context ->> 'run_id' !~ opaque_shape
               OR jsonb_typeof(COALESCE(run_context -> 'external_ids', '{}'::jsonb)) <> 'object'
               OR (SELECT count(*) FROM jsonb_object_keys(
                       COALESCE(run_context -> 'external_ids', '{}'::jsonb))) > 16
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_each(COALESCE(run_context -> 'external_ids', '{}'::jsonb)) AS id
                   WHERE id.key !~ '^[a-z][a-z0-9_]{0,63}$'
                      OR jsonb_typeof(id.value) <> 'string'
                      OR id.value #>> '{}' !~ opaque_shape
               ) THEN
                RAISE EXCEPTION 'run_context must be a run id and opaque external ids';
            END IF;
        END IF;
        IF NEW.payload_json IS NOT NULL
           AND (octet_length(NEW.payload_json) > <max_payload>
                OR jsonb_typeof(NEW.payload_json::jsonb) <> 'object') THEN
            RAISE EXCEPTION 'a stored payload must be a JSON object of at most <max_payload> bytes';
        END IF;
        IF NEW.status <> 'pending' OR NEW.decision IS NOT NULL OR NEW.resolved_by IS NOT NULL
           OR NEW.resolved_at IS NOT NULL OR NEW.consumed_at IS NOT NULL
           OR NEW.closed_at IS NOT NULL OR NEW.reason IS NOT NULL
           OR NEW.payload_purged_at IS NOT NULL THEN
            RAISE EXCEPTION 'a new approval request must be pending and undecided';
        END IF;
        IF NEW.created_at::timestamptz > db_now + interval '5 minutes'
           OR NEW.expires_at::timestamptz <= NEW.created_at::timestamptz
           OR NEW.expires_at::timestamptz > NEW.created_at::timestamptz + interval '168 hours' THEN
            RAISE EXCEPTION 'an approval request must start by now and live at most 7 days';
        END IF;
        RETURN NEW;
    END IF;

    IF ROW(NEW.id, NEW.action, NEW.summary, NEW.payload_sha256, NEW.requested_by,
           NEW.required_role, NEW.created_at, NEW.expires_at, NEW.run_context, NEW.delegates)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.action, OLD.summary, OLD.payload_sha256, OLD.requested_by,
           OLD.required_role, OLD.created_at, OLD.expires_at, OLD.run_context, OLD.delegates) THEN
        RAISE EXCEPTION 'an approval request''s identity and payload never change';
    END IF;

    -- The stored payload changes in one way only: purged, by the approver role, once the
    -- request is finished and past the retention floor. The hash it was bound to stays.
    is_purge := OLD.payload_json IS NOT NULL AND NEW.payload_json IS NULL;
    IF (NEW.payload_json IS DISTINCT FROM OLD.payload_json
        OR NEW.payload_purged_at IS DISTINCT FROM OLD.payload_purged_at) AND NOT is_purge THEN
        RAISE EXCEPTION 'an approval request''s identity and payload never change';
    END IF;
    IF is_purge THEN
        IF NOT as_approver THEN
            RAISE EXCEPTION 'only the approver role may purge a stored payload';
        END IF;
        IF OLD.status NOT IN ('consumed', 'rejected', 'cancelled', 'expired')
           OR NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION 'only the payload of a finished request may be purged';
        END IF;
        finished_at := CASE OLD.status WHEN 'consumed' THEN OLD.consumed_at
                                       WHEN 'rejected' THEN OLD.resolved_at
                                       ELSE OLD.closed_at END;
        IF finished_at IS NULL OR NOT_CANONICAL(finished_at)
           OR finished_at::timestamptz + make_interval(secs => <retention_floor>) > db_now THEN
            RAISE EXCEPTION 'a payload may be purged only after the retention floor';
        END IF;
        IF NEW.payload_purged_at IS NULL OR OLD.payload_purged_at IS NOT NULL
           OR NOT_CANONICAL(NEW.payload_purged_at)
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at,
                  NEW.closed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at,
                  OLD.closed_at) THEN
            RAISE EXCEPTION 'a purge sets payload_json to NULL and payload_purged_at only';
        END IF;
        NEW.payload_purged_at := db_stamp;
        RETURN NEW;
    END IF;
    is_expired := OLD.expires_at::timestamptz <= db_now;

    IF OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected') THEN
        IF NOT as_approver THEN
            RAISE EXCEPTION 'only the approver role may decide an approval request';
        END IF;
        -- Rows written before the guard existed (0.1.0a2) may carry any lifetime;
        -- such a request can be neither decided nor used, only cancelled or expired.
        is_overlong := NOT_CANONICAL(OLD.created_at) OR NOT_CANONICAL(OLD.expires_at)
            OR OLD.expires_at::timestamptz > OLD.created_at::timestamptz + interval '168 hours';
        IF is_expired OR is_overlong THEN
            RAISE EXCEPTION 'approval request % has expired or has no valid lifetime', OLD.id;
        END IF;
<binding_check>        IF NEW.decision IS DISTINCT FROM
               (CASE NEW.status WHEN 'approved' THEN 'approve' ELSE 'reject' END)
           OR NEW.resolved_by IS NULL OR NEW.resolved_by = OLD.requested_by
           OR jsonb_exists(OLD.delegates::jsonb, NEW.resolved_by)
           OR NEW.resolved_by !~ principal_shape
           OR NEW.resolved_at IS NULL OR NOT_CANONICAL(NEW.resolved_at)
           OR NEW.resolved_at::timestamptz < OLD.created_at::timestamptz
           OR NEW.resolved_at::timestamptz NOT BETWEEN db_now - interval '5 minutes'
                                                   AND db_now + interval '5 minutes'
           OR (NEW.reason IS NOT NULL
               AND (length(NEW.reason) NOT BETWEEN 1 AND 500 OR NEW.reason ~ unsafe_text))
           OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
           OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
            RAISE EXCEPTION 'a decision sets decision, resolved_by and resolved_at only';
        END IF;
        -- A rejection is a finish time, which the retention floor counts from: the
        -- database writes it (never before the request was created), as for closed_at.
        IF NEW.status = 'rejected' THEN
            NEW.resolved_at := to_char(GREATEST(db_now, OLD.created_at::timestamptz)
                                       AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'pending' AND NEW.status IN ('cancelled', 'expired') THEN
        IF NEW.status = 'cancelled' AND NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may cancel an approval request';
        END IF;
        IF NEW.status = 'expired' AND NOT (as_requester OR as_approver) THEN
            RAISE EXCEPTION 'only the requester or approver role may expire a request';
        END IF;
        IF NEW.status = 'expired' AND NOT is_expired THEN
            RAISE EXCEPTION 'approval request % has not expired yet', OLD.id;
        END IF;
        IF NEW.closed_at IS NULL OR NOT_CANONICAL(NEW.closed_at)
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at) THEN
            RAISE EXCEPTION 'closing a request sets closed_at only';
        END IF;
        NEW.closed_at := db_stamp;
        RETURN NEW;
    END IF;

    IF OLD.status = 'approved' AND NEW.status = 'expired' THEN
        IF NOT (as_requester OR as_approver) THEN
            RAISE EXCEPTION 'only the requester or approver role may expire a request';
        END IF;
        IF NOT is_expired THEN
            RAISE EXCEPTION 'approval request % has not expired yet', OLD.id;
        END IF;
        IF NEW.closed_at IS NULL OR NOT_CANONICAL(NEW.closed_at)
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at) THEN
            RAISE EXCEPTION 'closing a request sets closed_at only';
        END IF;
        NEW.closed_at := db_stamp;
        RETURN NEW;
    END IF;

    IF OLD.status = 'approved' AND NEW.status = 'consumed' THEN
        IF NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may consume an approval';
        END IF;
        -- Rows written before the guard existed (0.1.0a2) may carry any lifetime;
        -- such a request can be neither decided nor used, only cancelled or expired.
        is_overlong := NOT_CANONICAL(OLD.created_at) OR NOT_CANONICAL(OLD.expires_at)
            OR OLD.expires_at::timestamptz > OLD.created_at::timestamptz + interval '168 hours';
        IF is_expired OR is_overlong THEN
            RAISE EXCEPTION 'approval request % has expired or has no valid lifetime', OLD.id;
        END IF;
        IF NEW.consumed_at IS NULL OR NOT_CANONICAL(NEW.consumed_at)
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.closed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.closed_at) THEN
            RAISE EXCEPTION 'consuming an approval sets consumed_at only';
        END IF;
        NEW.consumed_at := db_stamp;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'approval request % cannot move from % to %', OLD.id, OLD.status, NEW.status;
END
"""


@dataclass(frozen=True)
class Grant:
    """One privilege a role holds on one of the library's tables, or one of its columns."""

    table: str
    role: str
    privilege: str
    column: str | None = None

    def __str__(self) -> str:
        on = f"{self.table}({self.column})" if self.column else self.table
        return f"{self.role} has {self.privilege} on {on}"


@dataclass(frozen=True)
class InstallReport:
    """What install_postgres_schema found.

    outside_layout lists every privilege on the library's tables held by a role
    other than the owner, the requester and the approver, or held by those two
    beyond their layout: an a2 app role's grants, say. The installer never
    revokes them; revoke them yourself once nothing uses them.

    unaudited_approvals lists approved, unconsumed requests that no
    approval.resolved audit event approves: what plain SQL could have approved
    before 0.1.0a3. closed_approvals lists those the run cancelled, when asked.
    closed_duplicates lists the pending requests the run cancelled, when asked, because
    another open request already held their requester, action and payload hash.
    backdated_finishes lists finished requests that still hold a payload and whose finish
    time is more than 5 minutes (the guard's clock skew) before their own creation or
    decision (at most 1000, by id). 0.1.0a4 let the role that closed a request write that
    time, so each of these is purgeable at once under any retention floor, whenever it
    really finished; since 0.1.0a5 the database writes the time. The installer changes
    nothing about them: check them before the first purge. Rows written under 0.1.0a4, which
    bounded none of these times, can be listed for honest skew between the requester's and
    the approver's hosts, so read the list as candidates. An empty list does not show that
    nothing was backdated: a time moved back but still after the request's own history is
    not detectable here.

    login_binding is whether the guard now binds resolved_by to the deciding login.
    unmapped_logins lists login roles that are members of the approver role and have no
    active mapping: with binding on, none of them can decide a request.
    """

    schema: str
    requester_role: str
    approver_role: str
    outside_layout: tuple[Grant, ...] = field(default=())
    unaudited_approvals: tuple[str, ...] = field(default=())
    closed_approvals: tuple[str, ...] = field(default=())
    closed_duplicates: tuple[str, ...] = field(default=())
    backdated_finishes: tuple[str, ...] = field(default=())
    login_binding: bool = False
    unmapped_logins: tuple[str, ...] = field(default=())

    def __str__(self) -> str:
        lines = [
            f"Installed in schema {self.schema}: requester role {self.requester_role}, "
            f"approver role {self.approver_role}."
        ]
        if self.outside_layout:
            lines.append("Grants outside the layout, to revoke once nothing uses them:")
            lines += [f"  {grant}" for grant in self.outside_layout]
        if self.unaudited_approvals:
            lines.append(
                "Approved, unused requests with no approval.resolved audit event "
                "(possibly approved by plain SQL before 0.1.0a3):"
            )
            lines += [f"  {request_id}" for request_id in self.unaudited_approvals]
        if self.closed_approvals:
            lines.append("Cancelled by this run, as asked:")
            lines += [f"  {request_id}" for request_id in self.closed_approvals]
        if self.closed_duplicates:
            lines.append("Pending duplicates cancelled by this run, as asked:")
            lines += [f"  {request_id}" for request_id in self.closed_duplicates]
        if self.backdated_finishes:
            lines.append(
                "Finished requests holding a payload whose finish time is more than 5 minutes "
                "before they were created or decided (0.1.0a4 let a client write it): a purge "
                "would take them at once, so check them first:"
            )
            lines += [f"  {request_id}" for request_id in self.backdated_finishes]
        if self.login_binding:
            lines.append("Login binding is on: resolved_by must be the deciding login's principal.")
            if self.unmapped_logins:
                lines.append("Approver logins with no mapping (they cannot decide requests):")
                lines += [f"  {login}" for login in self.unmapped_logins]
        return "\n".join(lines)


# Direct grants on a table and on its columns, from the ACLs. grantee 0 is PUBLIC.
GRANTS_SQL = """
SELECT g.grantee, g.privilege_type, NULL::text
FROM pg_class c, aclexplode(c.relacl) g
WHERE c.oid = to_regclass(?)
UNION ALL
SELECT g.grantee, g.privilege_type, a.attname::text
FROM pg_attribute a, aclexplode(a.attacl) g
WHERE a.attrelid = to_regclass(?) AND a.attnum > 0 AND NOT a.attisdropped
"""


def grant_statements(
    schema: str, role: str, layout: Mapping[str, Mapping[str, frozenset[str] | None]]
) -> Iterable[tuple[str, str]]:
    """(table, GRANT statement) pairs for a role's layout."""
    quoted_schema = identifier(schema, what="schema")
    quoted_role = "PUBLIC" if role == "PUBLIC" else identifier(role, what="role")
    for table, privileges in layout.items():
        for privilege, columns in privileges.items():
            on = f"({', '.join(sorted(columns))}) " if columns is not None else ""
            yield table, f"GRANT {privilege} {on}ON {quoted_schema}.{table} TO {quoted_role}"


def within_layout(grant: Grant, layout: Mapping[str, Mapping[str, frozenset[str] | None]]) -> bool:
    allowed = layout.get(grant.table, {})
    if grant.privilege not in allowed:
        return False
    columns = allowed[grant.privilege]
    return columns is None or (grant.column is not None and grant.column in columns)


class ConnectionSide:
    """Which run-time role a connection acts as."""

    REQUESTER: Final = "requester"
    APPROVER: Final = "approver"


# Every role that current_user or session_user can switch to, with or without
# inheriting its privileges, is checked: none may be a superuser, own the table,
# or be able to delete or truncate it.
_CONNECTION_SQL = """
WITH installed AS (
    SELECT ?::text AS requester, ?::text AS approver, to_regclass(?) AS approvals
)
SELECT
    EXISTS (
        SELECT 1 FROM pg_roles m
        WHERE (pg_has_role(current_user, m.oid, 'MEMBER')
               OR pg_has_role(session_user, m.oid, 'MEMBER'))
          AND (m.rolsuper OR m.oid = c.relowner
               OR has_table_privilege(m.oid, c.oid, 'DELETE')
               OR has_table_privilege(m.oid, c.oid, 'TRUNCATE'))
    ),
    pg_has_role(current_user, i.requester, 'MEMBER')
        OR pg_has_role(session_user, i.requester, 'MEMBER'),
    pg_has_role(current_user, i.approver, 'MEMBER')
        OR pg_has_role(session_user, i.approver, 'MEMBER'),
    pg_has_role(i.requester, i.approver, 'MEMBER')
        OR pg_has_role(i.approver, i.requester, 'MEMBER')
FROM installed i JOIN pg_class c ON c.oid = i.approvals
"""


async def require_postgres_version(session: "Session") -> None:
    """Raise ConfigError if the server is older than the library supports (16)."""
    version_num = int((await session.execute("SELECT current_setting('server_version_num')"))[0][0])
    if version_num < POSTGRES_MINIMUM_VERSION_NUM:
        raise ConfigError(
            f"This Postgres server is version {version_num // 10000}; agent-core 0.1.0a7 "
            f"needs {POSTGRES_MINIMUM_VERSION_NUM // 10000} or later."
        )


async def check_connection(session: "Session", schema: str) -> str:
    """Check the approvals schema and the connecting role; return the role's side.

    Raises ConfigError, before anything is written, when the schema predates
    0.1.0a3 or lost a guard trigger, when the requester and approver roles
    overlap or hold more than their layout allows on the decision columns, or
    when the connecting role is a superuser, the owner, able to delete rows,
    or a member of both roles or of neither.
    """
    await require_postgres_version(session)
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{APPROVALS_TABLE}"
    roles_table = f"{quoted_schema}.{ROLES_TABLE}"
    exists, has_roles = (
        await session.execute(
            "SELECT to_regclass(?) IS NOT NULL, to_regclass(?) IS NOT NULL", (table, roles_table)
        )
    )[0]
    if not exists:
        raise ConfigError(
            f"Table {schema}.{APPROVALS_TABLE} does not exist; install it with "
            "storage.install_postgres_schema as the owner role."
        )
    triggers = {
        row[0]
        for row in await session.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = to_regclass(?) AND NOT tgisinternal AND tgenabled <> 'D'",
            (table,),
        )
    }
    if not has_roles or not triggers >= APPROVALS_TRIGGERS:
        missing = sorted(APPROVALS_TRIGGERS - triggers)
        raise ConfigError(
            f"The approvals table in {schema} is not protected by the database "
            f"({'missing or disabled: ' + ', '.join(missing) if missing else 'no role table'}): "
            "it was created by agent-core 0.1.0a2, or its guard was removed. As the owner "
            "role, run install_postgres_schema from 0.1.0a7 with the requester and approver "
            "roles; it upgrades the schema in place and keeps every row."
        )
    revision = await _guard_revision(session, table)
    if revision != GUARD_REVISION:
        raise ConfigError(
            f"The approvals guard in {schema} is revision {revision or 'older than 5'}; this "
            f"release needs {GUARD_REVISION}. As the owner role, run install_postgres_schema "
            "from 0.1.0a7 with the requester and approver roles: it upgrades the schema in "
            "place and keeps every row (see docs/upgrading.md)."
        )
    await _require_open_request_index(session, schema, table)
    requester_role, approver_role = (
        await session.execute(f"SELECT requester_role, approver_role FROM {roles_table}")
    )[0]
    can_change, is_requester, is_approver, overlap = (
        await session.execute(_CONNECTION_SQL, (requester_role, approver_role, table))
    )[0]
    if overlap or requester_role == approver_role:
        raise ConfigError(
            f"The requester role {requester_role} and approver role {approver_role} overlap, "
            "so a requester could approve. Use two unrelated roles."
        )
    if can_change:
        raise ConfigError(
            "This connection's role, or a role it can switch to, is a superuser, owns the "
            "approvals table, or can delete from or truncate it. Connect as the requester or "
            "the approver role."
        )
    if is_requester == is_approver:
        raise ConfigError(
            "This connection's role must be a member of exactly one of "
            f"{requester_role} and {approver_role}; it is a member of "
            f"{'both' if is_requester else 'neither'}."
        )
    await _check_layout(session, table, requester_role, approver_role)
    await _check_connecting_roles(session, table, as_requester=bool(is_requester))
    await refuse_requester_create(session, requester_role, schema)
    if await login_binding_enabled(session, schema):
        await _require_login_mapping(
            session, schema, requester_role, approver_role, as_approver=bool(is_approver)
        )
    return ConnectionSide.REQUESTER if is_requester else ConnectionSide.APPROVER


async def _guard_source(session: "Session", table: str) -> str:
    rows = await session.execute(
        "SELECT p.prosrc FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE t.tgrelid = to_regclass(?) AND t.tgname = ?",
        (table, APPROVALS_GUARD_TRIGGER),
    )
    return str(rows[0][0]) if rows else ""


async def _guard_revision(session: "Session", table: str) -> int | None:
    """The revision comment inside the installed guard function, or None if it has none."""
    found = re.search(r"-- agent-core guard revision (\d+)", await _guard_source(session, table))
    return int(found[1]) if found else None


async def audit_trigger_revision(session: "Session", table: str) -> int | None:
    """The revision comment inside the installed audit insert trigger, or None if it has none."""
    rows = await session.execute(
        "SELECT p.prosrc FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE t.tgrelid = to_regclass(?) AND t.tgname = ?",
        (table, AUDIT_APPEND_TRIGGER),
    )
    found = (
        re.search(r"-- agent-core audit trigger revision (\d+)", str(rows[0][0])) if rows else None
    )
    return int(found[1]) if found else None


async def login_binding_enabled(session: "Session", schema: str) -> bool | None:
    """Whether the installed guard binds resolved_by to the deciding login; None if no guard."""
    table = f"{identifier(schema, what='schema')}.{APPROVALS_TABLE}"
    source = await _guard_source(session, table)
    found = re.search(rf"{re.escape(BINDING_MARKER)} (on|off)\b", source)
    return None if found is None else found[1] == "on"


async def bound_principal(session: "Session", schema: str) -> str | None:
    """The principal the owner mapped to the connecting login (session_user), or None."""
    function = f"{identifier(schema, what='schema')}.{BOUND_PRINCIPAL_FUNCTION}"
    principal = (await session.execute(f"SELECT {function}()"))[0][0]
    return None if principal is None else str(principal)


async def _require_login_mapping(
    session: "Session",
    schema: str,
    requester_role: str,
    approver_role: str,
    *,
    as_approver: bool,
) -> None:
    """With binding on, refuse a setup whose mapping is missing, open to writes, or unmapped.

    The table and its guard must exist and be enabled; neither installed role, nor any role
    the connection can switch to, may write the table (only the owner maps logins); and an
    approver-side connection must have an active mapping for its own login.
    """
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{LOGINS_TABLE}"
    function = f"{quoted_schema}.{BOUND_PRINCIPAL_FUNCTION}"
    present = (
        await session.execute(
            "SELECT to_regclass(?) IS NOT NULL, to_regprocedure(?) IS NOT NULL",
            (table, f"{function}()"),
        )
    )[0]
    triggers = {
        row[0]
        for row in await session.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = to_regclass(?) AND NOT tgisinternal AND tgenabled <> 'D'",
            (table,),
        )
    }
    if not all(present) or not triggers >= LOGINS_TRIGGERS:
        raise ConfigError(
            f"Login binding is on in {schema}, but its login mapping is missing or unprotected "
            f"({LOGINS_TABLE}, {BOUND_PRINCIPAL_FUNCTION}() and the table's triggers). As the "
            "owner role, run install_postgres_schema from 0.1.0a7, then map each approver "
            "login with bind_approver_login."
        )
    await require_logins_made_by_owner(session, schema)
    guard_source = (
        await session.execute(
            "SELECT prosrc FROM pg_proc WHERE oid = to_regprocedure(?)",
            (f"{quoted_schema}.{LOGINS_TABLE}_guard()",),
        )
    )[0][0]
    found = re.search(rf"{re.escape(LOGINS_GUARD_MARKER)} (\d+)", guard_source)
    if found is None or int(found[1]) < LOGINS_GUARD_REVISION:
        raise ConfigError(
            f"The login mapping guard in {schema} is out of date: it does not yet refuse a "
            "write by a role that reaches the table without a grant, such as a member of "
            "pg_write_all_data. As the owner role, run install_postgres_schema from 0.1.0."
        )
    roles = [requester_role, approver_role] + [
        row[0] for row in await session.execute(_CONNECTING_ROLES)
    ]
    for role in roles:
        writable = (
            await session.execute(
                "SELECT " + " OR ".join(f"has_table_privilege(?, ?, '{p}')" for p in _WRITES),
                tuple(value for _ in _WRITES for value in (role, table)),
            )
        )[0][0]
        if writable:
            raise ConfigError(
                f"{role} can write the login mapping table in {schema}. Only the owner role may "
                "map logins; revoke that grant."
            )
    if as_approver and await bound_principal(session, schema) is None:
        login = (await session.execute("SELECT session_user::text"))[0][0]
        raise ConfigError(
            f"Login binding is on in {schema}, and the login {login} has no principal mapped. "
            "As the owner role, map it with bind_approver_login before it decides requests."
        )


_LOGINS_ACL_SQL = """
SELECT CASE g.grantee WHEN 0 THEN 'PUBLIC' ELSE g.grantee::regrole::text END,
       g.grantor::regrole::text
FROM pg_class c, aclexplode(c.relacl) g
WHERE c.oid = to_regclass(?) AND g.grantee <> c.relowner
UNION ALL
SELECT CASE g.grantee WHEN 0 THEN 'PUBLIC' ELSE g.grantee::regrole::text END,
       g.grantor::regrole::text
FROM pg_attribute a, aclexplode(a.attacl) g, pg_class c
WHERE a.attrelid = to_regclass(?) AND c.oid = a.attrelid AND a.attnum > 0
  AND NOT a.attisdropped AND g.grantee <> c.relowner
"""


async def require_logins_made_by_owner(session: "Session", schema: str) -> None:
    """Refuse a mapping table or lookup function the owner did not make, or others can reach.

    The installer creates the table only if it is absent, so a table another role made first
    would be adopted, with that role as its owner. The table and the function must have the
    approvals table's owner, the function must be SECURITY DEFINER with a pinned search_path,
    and no role but the owner may hold any table or column privilege on the table (default
    privileges and column grants included).
    """
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{LOGINS_TABLE}"
    function = f"{quoted_schema}.{BOUND_PRINCIPAL_FUNCTION}()"
    owners = (
        await session.execute(
            "SELECT t.relowner = a.relowner, p.proowner = a.relowner, p.prosecdef, "
            "COALESCE(p.proconfig::text[] @> ARRAY['search_path=pg_catalog, pg_temp'], false) "
            "FROM pg_class t, pg_class a, pg_proc p "
            "WHERE t.oid = to_regclass(?) AND a.oid = to_regclass(?) "
            "AND p.oid = to_regprocedure(?)",
            (table, f"{quoted_schema}.{APPROVALS_TABLE}", function),
        )
    )[0:1]
    if not owners or not all(owners[0]):
        raise ConfigError(
            f"The login mapping table or {BOUND_PRINCIPAL_FUNCTION}() in {schema} was not made by "
            "the approvals table's owner, or the function is not SECURITY DEFINER with a pinned "
            "search_path. Another role may have created them first. As the owner role, drop "
            "them and run install_postgres_schema again."
        )
    grants = [(row[0], row[1]) for row in await session.execute(_LOGINS_ACL_SQL, (table, table))]
    if grants:
        grantees = ", ".join(sorted({grantee for grantee, _ in grants}))
        grantors = ", ".join(sorted({grantor for _, grantor in grants}))
        raise ConfigError(
            f"{grantees} holds a privilege on the login mapping table in {schema}, directly, by "
            f"column or by default privileges. Only the owner may. Revoke it as the role that "
            f"granted it ({grantors}): the owner's REVOKE removes only the owner's own grants."
        )


_WRITES: Final = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


async def payload_retention_floor_seconds(session: "Session", schema: str) -> int:
    """The shortest retention, in seconds, the installed guard allows a purge."""
    table = f"{identifier(schema, what='schema')}.{APPROVALS_TABLE}"
    found = re.search(
        r"-- agent-core payload retention floor (\d+) seconds", await _guard_source(session, table)
    )
    if found is None:
        raise ConfigError(
            f"The approvals guard in {schema} carries no payload retention floor. As the owner "
            "role, run install_postgres_schema from 0.1.0a7."
        )
    return int(found[1])


async def _require_open_request_index(session: "Session", schema: str, table: str) -> None:
    """Raise ConfigError unless the unique open-request index is present, valid and as written."""
    index = f"{identifier(schema, what='schema')}.{OPEN_REQUEST_INDEX}"
    rows = await session.execute(
        "SELECT i.indisunique AND i.indisvalid AND i.indisready, "
        "pg_get_expr(i.indpred, i.indrelid), "
        "ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true) "
        "FROM generate_series(1, i.indnkeyatts) k ORDER BY k) "
        "FROM pg_index i WHERE i.indexrelid = to_regclass(?) AND i.indrelid = to_regclass(?)",
        (index, table),
    )
    predicate_ok = False
    if rows:
        usable, predicate, columns = rows[0]
        predicate_ok = (
            bool(usable)
            and predicate is not None
            and all(f"'{status}'" in predicate for status in OPEN_REQUEST_STATUSES)
            and not any(
                f"'{status}'" in predicate
                for status in ("consumed", "rejected", "cancelled", "expired")
            )
            and tuple(columns) == OPEN_REQUEST_COLUMNS
        )
    if not predicate_ok:
        raise ConfigError(
            f"The approvals table in {schema} has no valid unique index {OPEN_REQUEST_INDEX} "
            "on (requested_by, action, payload_sha256) for pending and approved requests, so "
            "two identical submits could both be approved. As the owner role, run "
            "install_postgres_schema from 0.1.0a7 with the requester and approver roles."
        )


# Every role current_user or session_user can switch to, as in the role check.
_CONNECTING_ROLES = """
SELECT m.rolname FROM pg_roles m
WHERE pg_has_role(current_user, m.oid, 'MEMBER') OR pg_has_role(session_user, m.oid, 'MEMBER')
ORDER BY m.rolname
"""


async def _check_connecting_roles(session: "Session", table: str, *, as_requester: bool) -> None:
    """Refuse a connection whose own roles hold what its side's role must not.

    _check_layout looks at the two installed roles; this looks at the login and
    every role it can switch to, which may hold direct grants of their own (an
    a2 app role made a member of the requester role, say). The guard still
    refuses such writes; this makes the setup fail loudly first.
    """
    for (role,) in await session.execute(_CONNECTING_ROLES):
        if as_requester:
            rights = (
                await session.execute(
                    "SELECT "
                    + " OR ".join(
                        "has_column_privilege(?, ?, ?, 'UPDATE')"
                        for _ in REQUESTER_FORBIDDEN_COLUMNS
                    ),
                    tuple(
                        value
                        for column in REQUESTER_FORBIDDEN_COLUMNS
                        for value in (role, table, column)
                    ),
                )
            )[0][0]
            what = "update a decision column"
        else:
            rights = (
                await session.execute("SELECT has_table_privilege(?, ?, 'INSERT')", (role, table))
            )[0][0]
            what = "insert approval requests"
        if rights:
            raise ConfigError(
                f"This connection can act as {role}, which can {what} on the approvals "
                "table. Revoke that grant or connect as a role without it."
            )


async def refuse_requester_create(session: "Session", requester_role: str, schema: str) -> None:
    """Raise ConfigError if any role acting as the requester can create objects.

    The library pins its own search_path, but the approver side may run other
    code too, and a function or table the requester planted where that code
    looks (the install schema, public, or a new schema named after the approver
    role, which a default "$user", public path searches) would run with the
    approver's rights. Every non-superuser role that is the requester role or a
    member of it is checked: no CREATE on the install schema or public, and no
    CREATE on the database. Postgres 14 and clusters upgraded from it let PUBLIC
    create in public by default.
    """
    rows = await session.execute(
        "SELECT r.rolname, n.nspname FROM pg_roles r "
        "JOIN pg_namespace n ON n.nspname IN (?, 'public') "
        "WHERE NOT r.rolsuper AND pg_has_role(r.oid, ?, 'MEMBER') "
        "AND has_schema_privilege(r.oid, n.oid, 'CREATE') "
        "UNION ALL "
        "SELECT r.rolname, 'the database' FROM pg_roles r "
        "WHERE NOT r.rolsuper AND pg_has_role(r.oid, ?, 'MEMBER') "
        "AND has_database_privilege(r.oid, current_database(), 'CREATE') "
        "ORDER BY 1, 2",
        (schema, requester_role, requester_role),
    )
    if rows:
        role, where = rows[0]
        target = where if where == "the database" else f"schema {where}"
        raise ConfigError(
            f"The requester role {requester_role}, through {role}, can create objects in "
            f"{target}, where code on the approver side could pick them up. Revoke it, e.g. "
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC (the default before Postgres 15)."
        )


async def _check_layout(
    session: "Session", table: str, requester_role: str, approver_role: str
) -> None:
    """Refuse grants that would let a requester decide or an approver submit."""
    decision_rights = (
        await session.execute(
            "SELECT "
            + ", ".join(
                "has_column_privilege(?, ?, ?, 'UPDATE')" for _ in REQUESTER_FORBIDDEN_COLUMNS
            ),
            tuple(
                value
                for column in REQUESTER_FORBIDDEN_COLUMNS
                for value in (requester_role, table, column)
            ),
        )
    )[0]
    writable = [
        column
        for column, can in zip(REQUESTER_FORBIDDEN_COLUMNS, decision_rights, strict=True)
        if can
    ]
    if writable:
        raise ConfigError(
            f"The requester role {requester_role} can update {', '.join(writable)} on the "
            "approvals table, so it could record a decision or destroy a payload. Revoke that "
            "grant."
        )
    if (
        await session.execute("SELECT has_table_privilege(?, ?, 'INSERT')", (approver_role, table))
    )[0][0]:
        raise ConfigError(
            f"The approver role {approver_role} can insert approval requests. Revoke that grant."
        )
