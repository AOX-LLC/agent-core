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
    (insert)  pending    requester               undecided; lifetime starts by now, at most 7 days
    pending   approved   approver                decision 'approve', resolved_by set and not the
                                                 requester, resolved_at set, not expired
    pending   rejected   approver                as approved, with decision 'reject'
    pending   cancelled  requester               closed_at set
    pending   expired    requester or approver   closed_at set, expires_at already past
    approved  consumed   requester               consumed_at set, not expired

Every other change is refused, as are DELETE and TRUNCATE, and no update may
touch a request's identity, payload hash, requester, required role, lifetime,
run context or delegates.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from aox_agent_core.errors import ConfigError

AUDIT_TABLE: Final = "agent_core_audit"
APPROVALS_TABLE: Final = "agent_core_approvals"
ROLES_TABLE: Final = "agent_core_approval_roles"

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
    ROLES_TABLE: {"SELECT": None},
}
APPROVER_LAYOUT: Final[Mapping[str, Mapping[str, frozenset[str] | None]]] = {
    AUDIT_TABLE: {"SELECT": None, "INSERT": None},
    APPROVALS_TABLE: {
        "SELECT": None,
        "UPDATE": frozenset(
            {"status", "decision", "resolved_by", "resolved_at", "reason", "closed_at"}
        ),
    },
    ROLES_TABLE: {"SELECT": None},
}
# Decision columns the requester role must never be able to write.
DECISION_COLUMNS: Final = ("decision", "resolved_by", "resolved_at", "reason")


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
            db_role TEXT
        )""",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS db_role TEXT",
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
        # db_role is set here, from the database's own idea of who is inserting,
        # whatever the insert supplied; the update trigger then keeps it fixed.
        f"""CREATE OR REPLACE FUNCTION {functions}.{AUDIT_TABLE}_append_at_end()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE last_seq bigint;
        BEGIN
            EXECUTE format('SELECT COALESCE(MAX(seq), 0) FROM %I.%I',
                           TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO last_seq;
            IF NEW.seq <> last_seq + 1 THEN
                RAISE EXCEPTION '{AUDIT_TABLE} is append-only';
            END IF;
            NEW.db_role := current_user;
            RETURN NEW;
        END $$""",
        f"""CREATE OR REPLACE TRIGGER {AUDIT_APPEND_TRIGGER} BEFORE INSERT ON {table}
        FOR EACH ROW EXECUTE FUNCTION {functions}.{AUDIT_TABLE}_append_at_end()""",
        f"REVOKE ALL ON {table} FROM PUBLIC",
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
            delegates TEXT NOT NULL DEFAULT '[]'
        )""",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS closed_at TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS delegates TEXT NOT NULL DEFAULT '[]'",
        f"""CREATE INDEX IF NOT EXISTS agent_core_approvals_pending
        ON {table} (status, created_at, id)""",
        f"""CREATE TABLE IF NOT EXISTS {roles} (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            requester_role TEXT NOT NULL,
            approver_role TEXT NOT NULL
        )""",
        f"REVOKE ALL ON {table} FROM PUBLIC",
        f"REVOKE ALL ON {roles} FROM PUBLIC",
    )


def approvals_guard_ddl(schema: str, requester_role: str, approver_role: str) -> tuple[str, ...]:
    """The guard function and its triggers.

    The role names are written into the guard itself, so it never depends on what
    the role it is checking may read.
    """
    quoted_schema = identifier(schema, what="schema")
    table = f"{quoted_schema}.{APPROVALS_TABLE}"
    # identifier() admits only [a-z0-9_], so the names are safe inside quotes.
    identifier(requester_role, what="role")
    identifier(approver_role, what="role")
    guard_body = _GUARD_BODY.replace("'<requester>'", f"'{requester_role}'").replace(
        "'<approver>'", f"'{approver_role}'"
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


# The guard carries the installed role names as literals. A role counts as the
# requester only if neither current_user nor session_user can act as the
# approver, and the other way round, so SET ROLE cannot cross sides. A superuser
# is a member of every role, so it is neither, and is refused.
_GUARD_BODY = """
DECLARE
    requester_role text := '<requester>';
    approver_role text := '<approver>';
    as_requester boolean;
    as_approver boolean;
    db_now timestamptz := statement_timestamp();
    is_expired boolean;
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
        IF NEW.status <> 'pending' OR NEW.decision IS NOT NULL OR NEW.resolved_by IS NOT NULL
           OR NEW.resolved_at IS NOT NULL OR NEW.consumed_at IS NOT NULL
           OR NEW.closed_at IS NOT NULL OR NEW.reason IS NOT NULL THEN
            RAISE EXCEPTION 'a new approval request must be pending and undecided';
        END IF;
        IF NEW.created_at::timestamptz > db_now + interval '5 minutes'
           OR NEW.expires_at::timestamptz <= NEW.created_at::timestamptz
           OR NEW.expires_at::timestamptz > NEW.created_at::timestamptz + interval '7 days' THEN
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
    is_expired := OLD.expires_at::timestamptz <= db_now;

    IF OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected') THEN
        IF NOT as_approver THEN
            RAISE EXCEPTION 'only the approver role may decide an approval request';
        END IF;
        IF is_expired THEN
            RAISE EXCEPTION 'approval request % has expired', OLD.id;
        END IF;
        IF NEW.decision IS DISTINCT FROM
               (CASE NEW.status WHEN 'approved' THEN 'approve' ELSE 'reject' END)
           OR NEW.resolved_by IS NULL OR NEW.resolved_by = OLD.requested_by
           OR NEW.resolved_at IS NULL
           OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
           OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
            RAISE EXCEPTION 'a decision sets decision, resolved_by and resolved_at only';
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
        IF NEW.closed_at IS NULL
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at) THEN
            RAISE EXCEPTION 'closing a request sets closed_at only';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'approved' AND NEW.status = 'consumed' THEN
        IF NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may consume an approval';
        END IF;
        IF is_expired THEN
            RAISE EXCEPTION 'approval request % has expired', OLD.id;
        END IF;
        IF NEW.consumed_at IS NULL
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.closed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.closed_at) THEN
            RAISE EXCEPTION 'consuming an approval sets consumed_at only';
        END IF;
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
    """

    schema: str
    requester_role: str
    approver_role: str
    outside_layout: tuple[Grant, ...] = field(default=())

    def __str__(self) -> str:
        lines = [
            f"Installed in schema {self.schema}: requester role {self.requester_role}, "
            f"approver role {self.approver_role}."
        ]
        if self.outside_layout:
            lines.append("Grants outside the layout, to revoke once nothing uses them:")
            lines += [f"  {grant}" for grant in self.outside_layout]
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
    quoted_role = identifier(role, what="role")
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
