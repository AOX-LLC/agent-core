"""The Postgres approvals schema, enforced by the database itself.

Every test here talks plain SQL as one role, the way someone holding only that
role's credentials could, and checks what the database allows. The library is
not involved, except to install the schema.
"""

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from aox_agent_core.approvals import Decision, Principal, PrincipalKind
from aox_agent_core.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditRecord,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import Grant, install_postgres_schema
from databases import (
    APPROVER_ROLE,
    LEGACY_APP_ROLE,
    REQUESTER_ROLE,
    ControlDatabase,
    Raw,
    postgres_database,
    split_queue,
)
from test_approval_payload import raw_request
from test_approvals import REQUESTER

A2_SCHEMA = Path(__file__).parent / "fixtures" / "postgres" / "a2_schema.sql"
STATES = ("pending", "approved", "rejected", "consumed", "cancelled", "expired")
ALLOWED = {
    ("pending", "approved", "approver"),
    ("pending", "rejected", "approver"),
    ("pending", "cancelled", "requester"),
    ("pending", "expired", "requester"),
    ("pending", "expired", "approver"),
    ("approved", "consumed", "requester"),
    ("approved", "expired", "requester"),
    ("approved", "expired", "approver"),
}
IMMUTABLE = (
    "action",
    "summary",
    "payload_sha256",
    "requested_by",
    "required_role",
    "created_at",
    "expires_at",
    "run_context",
    "delegates",
)


@pytest.fixture
def pg() -> Iterator[ControlDatabase]:
    with postgres_database() as database:
        yield database


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


NOW = datetime.now(UTC)
COLUMNS_FOR_STATE: dict[str, dict[str, str | None]] = {
    "pending": {},
    "approved": {"decision": "approve", "resolved_by": "user-17", "resolved_at": stamp(NOW)},
    "rejected": {"decision": "reject", "resolved_by": "user-17", "resolved_at": stamp(NOW)},
    "consumed": {
        "decision": "approve",
        "resolved_by": "user-17",
        "resolved_at": stamp(NOW),
        "consumed_at": stamp(NOW),
    },
    "cancelled": {"closed_at": stamp(NOW)},
    "expired": {"closed_at": stamp(NOW)},
}


def literal(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def planted(database: ControlDatabase, status: str, *, expired: bool = False) -> str:
    """A request in `status`, written past the guard as a superuser replicating rows."""
    request_id = str(uuid4())
    created = NOW - timedelta(hours=3 if expired else 0)
    expires = created + timedelta(hours=1)
    columns = {
        "id": request_id,
        "action": "crm.update_contact",
        "summary": "Update the sample contact",
        # Distinct per row: at most one request may be open for a requester, action and hash.
        "payload_sha256": uuid4().hex * 2,
        "requested_by": "agent-intake",
        "required_role": "ops.approver",
        "created_at": stamp(created),
        "expires_at": stamp(expires),
        "status": status,
        **COLUMNS_FOR_STATE[status],
    }
    database.superuser_raw(
        "SET session_replication_role = replica; "
        f"INSERT INTO {database.schema}.agent_core_approvals ({', '.join(columns)}) "
        f"VALUES ({', '.join(literal(value) for value in columns.values())})"
    )
    return request_id


def status_of(database: ControlDatabase, request_id: str) -> str:
    rows = database.raw(
        f"SELECT status FROM {database.schema}.agent_core_approvals WHERE id = '{request_id}'"
    )
    return str(rows[0][0])


def refused(run: Raw, sql: str) -> bool:
    try:
        run(sql)
    except psycopg.Error:
        return True
    return False


def as_role(database: ControlDatabase, role: str) -> Raw:
    run = database.requester_raw if role == "requester" else database.approver_raw
    assert run is not None
    return run


def transition_sql(request_id: str, to: str, *, status_only: bool) -> str:
    extra: dict[str, dict[str, str | None]] = {
        "approved": COLUMNS_FOR_STATE["approved"],
        "rejected": COLUMNS_FOR_STATE["rejected"],
        "consumed": {"consumed_at": stamp(NOW)},
        "cancelled": {"closed_at": stamp(NOW)},
        "expired": {"closed_at": stamp(NOW)},
        "pending": {},
    }
    changes: dict[str, str | None] = {"status": to}
    if not status_only:
        changes.update(extra[to])
    assignments = ", ".join(f"{column} = {literal(value)}" for column, value in changes.items())
    return f"UPDATE agent_core_approvals SET {assignments} WHERE id = '{request_id}'"


# The transition table


@pytest.mark.parametrize(
    ("from_state", "to_state", "role"),
    list(itertools.product(STATES, STATES, ("requester", "approver"))),
)
def test_only_the_listed_transitions_succeed(
    pg: ControlDatabase, from_state: str, to_state: str, role: str
) -> None:
    # Expiry needs a request whose lifetime is over; everything else, one still live.
    request_id = planted(pg, from_state, expired=to_state == "expired")
    run = as_role(pg, role)
    allowed = (from_state, to_state, role) in ALLOWED

    full = refused(run, transition_sql(request_id, to_state, status_only=False))
    if allowed:
        assert not full, f"{role} could not move {from_state} to {to_state}"
        assert status_of(pg, request_id) == to_state
        return
    assert full, f"{role} moved {from_state} to {to_state}"
    assert refused(run, transition_sql(request_id, to_state, status_only=True))
    assert status_of(pg, request_id) == from_state


@pytest.mark.parametrize("to_state", ["approved", "rejected", "consumed"])
def test_an_expired_request_cannot_be_decided_or_consumed(
    pg: ControlDatabase, to_state: str
) -> None:
    from_state = "approved" if to_state == "consumed" else "pending"
    role = "requester" if to_state == "consumed" else "approver"
    request_id = planted(pg, from_state, expired=True)

    assert refused(as_role(pg, role), transition_sql(request_id, to_state, status_only=False))
    assert status_of(pg, request_id) == from_state


@pytest.mark.parametrize("role", ["requester", "approver"])
def test_a_live_request_cannot_be_expired(pg: ControlDatabase, role: str) -> None:
    request_id = planted(pg, "pending")

    assert refused(as_role(pg, role), transition_sql(request_id, "expired", status_only=False))


def test_an_approver_cannot_approve_on_the_requesters_behalf(pg: ControlDatabase) -> None:
    request_id = planted(pg, "pending")
    assert pg.approver_raw is not None

    assert refused(
        pg.approver_raw,
        "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve', "
        f"resolved_by = 'agent-intake', resolved_at = '{stamp(NOW)}' WHERE id = '{request_id}'",
    )


# The requester role cannot reach an approved state


@pytest.mark.parametrize(
    "attack",
    [
        "UPDATE agent_core_approvals SET status = 'approved' WHERE id = '{id}'",
        "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve', "
        "resolved_by = 'user-17', resolved_at = '{now}' WHERE id = '{id}'",
        "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by, "
        "required_role, created_at, expires_at, status, decision, resolved_by, resolved_at) "
        "VALUES ('{new}', 'crm.update_contact', 's', '{hash}', 'agent-intake', 'ops.approver', "
        "'{now}', '{later}', 'approved', 'approve', 'user-17', '{now}')",
        "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by, "
        "required_role, created_at, expires_at, status) VALUES ('{id}', 'crm.update_contact', "
        "'s', '{hash}', 'agent-intake', 'ops.approver', '{now}', '{later}', 'pending') "
        "ON CONFLICT (id) DO UPDATE SET status = 'approved'",
        "DELETE FROM agent_core_approvals WHERE id = '{id}'",
        "TRUNCATE agent_core_approvals",
        "UPDATE agent_core_approval_roles SET approver_role = '" + REQUESTER_ROLE + "'",
        "ALTER TABLE agent_core_approvals DISABLE TRIGGER agent_core_approvals_guard",
        "SET ROLE " + APPROVER_ROLE,
        "CREATE TRIGGER sneak BEFORE UPDATE ON agent_core_approvals "
        "FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    ],
    ids=[
        "status-only",
        "full-decision",
        "insert-approved",
        "upsert",
        "delete",
        "truncate",
        "rewrite-roles",
        "disable-guard",
        "set-role",
        "add-trigger",
    ],
)
def test_the_requester_role_cannot_approve_by_any_route(pg: ControlDatabase, attack: str) -> None:
    request_id = planted(pg, "pending")
    assert pg.requester_raw is not None
    sql = attack.format(
        id=request_id,
        new=uuid4(),
        now=stamp(NOW),
        later=stamp(NOW + timedelta(hours=1)),
        hash="a" * 64,
    )

    assert refused(pg.requester_raw, sql)
    assert status_of(pg, request_id) == "pending"
    assert pg.raw("SELECT count(*) FROM agent_core_approvals WHERE status = 'approved'") == [(0,)]


def test_a_new_request_must_be_pending_short_lived_and_not_future_dated(
    pg: ControlDatabase,
) -> None:
    requester = pg.requester_raw
    assert requester is not None
    template = (
        "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by, "
        "required_role, created_at, expires_at, status) VALUES ('{id}', 'crm.update_contact', "
        "'s', '{hash}', 'agent-intake', 'ops.approver', '{created}', '{expires}', 'pending')"
    )

    def insert(created: datetime, expires: datetime) -> bool:
        return refused(
            requester,
            template.format(
                id=uuid4(), hash="a" * 64, created=stamp(created), expires=stamp(expires)
            ),
        )

    assert not insert(NOW, NOW + timedelta(hours=1))
    assert insert(NOW, NOW + timedelta(days=8))
    assert insert(NOW, NOW - timedelta(seconds=1))
    assert insert(NOW + timedelta(hours=1), NOW + timedelta(hours=2))


# The approver role


@pytest.mark.parametrize(
    "attack",
    [
        "UPDATE agent_core_approvals SET status = 'consumed', consumed_at = '{now}' "
        "WHERE id = '{approved}'",
        "UPDATE agent_core_approvals SET status = 'cancelled', closed_at = '{now}' "
        "WHERE id = '{pending}'",
        "DELETE FROM agent_core_approvals WHERE id = '{pending}'",
        "TRUNCATE agent_core_approvals",
        "UPDATE agent_core_approvals SET payload_sha256 = '{other}' WHERE id = '{pending}'",
        "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by, "
        "required_role, created_at, expires_at, status) VALUES ('{new}', 'crm.update_contact', "
        "'s', '{other}', 'agent-intake', 'ops.approver', '{now}', '{later}', 'pending')",
    ],
    ids=["consume", "cancel", "delete", "truncate", "payload-hash", "insert"],
)
def test_the_approver_role_can_only_decide(pg: ControlDatabase, attack: str) -> None:
    pending = planted(pg, "pending")
    approved = planted(pg, "approved")
    assert pg.approver_raw is not None
    sql = attack.format(
        pending=pending,
        approved=approved,
        new=uuid4(),
        now=stamp(NOW),
        later=stamp(NOW + timedelta(hours=1)),
        other="b" * 64,
    )

    assert refused(pg.approver_raw, sql)
    assert (status_of(pg, pending), status_of(pg, approved)) == ("pending", "approved")


@pytest.mark.parametrize("column", IMMUTABLE)
def test_no_update_changes_what_was_requested_even_with_a_broad_grant(
    pg: ControlDatabase, column: str
) -> None:
    # A login role that is the approver and was granted UPDATE on every column by
    # mistake: the grant would allow the change, the guard still refuses it.
    url = pg.login_role(
        f"GRANT {APPROVER_ROLE} TO {{role}}",
        "GRANT SELECT, UPDATE ON agent_core_approvals TO {role}",
    )
    request_id = planted(pg, "pending")
    value = {"created_at": stamp(NOW - timedelta(minutes=1)), "expires_at": stamp(NOW)}.get(
        column, "changed"
    )

    with (
        psycopg.connect(url, autocommit=True) as connection,
        pytest.raises(psycopg.Error, match="identity and payload never change"),
    ):
        connection.execute(
            f"UPDATE agent_core_approvals SET status = 'approved', decision = 'approve', "
            f"resolved_by = 'user-17', resolved_at = '{stamp(NOW)}', "
            f"{column} = {literal(value)} WHERE id = '{request_id}'"
        )


@pytest.mark.parametrize("inherit", [True, False], ids=["inherit", "noinherit"])
def test_a_login_in_both_roles_is_neither_even_after_set_role(
    pg: ControlDatabase, inherit: bool
) -> None:
    assert pg.superuser_url is not None
    role = f"agent_core_probe_{uuid4().hex[:8]}"
    pg.superuser_raw(f"CREATE ROLE {role} LOGIN {'INHERIT' if inherit else 'NOINHERIT'}")
    pg.roles.append(role)
    pg.superuser_raw(f"GRANT {REQUESTER_ROLE}, {APPROVER_ROLE} TO {role}")
    request_id = planted(pg, "pending")
    approve = transition_sql(request_id, "approved", status_only=False)
    url = pg.superuser_url.replace("postgres@", f"{role}@", 1)

    with psycopg.connect(url, autocommit=True) as connection:
        # Without inherited privileges the grant already refuses; with them, the guard.
        with pytest.raises(psycopg.Error, match="only the approver role" if inherit else None):
            connection.execute(approve)
        connection.execute(f"SET ROLE {APPROVER_ROLE}")
        with pytest.raises(psycopg.Error, match="only the approver role"):
            connection.execute(approve)

    assert status_of(pg, request_id) == "pending"


def test_the_owner_cannot_delete_or_decide_without_dropping_the_guard(pg: ControlDatabase) -> None:
    request_id = planted(pg, "pending")

    assert refused(pg.raw, f"DELETE FROM agent_core_approvals WHERE id = '{request_id}'")
    assert refused(pg.raw, transition_sql(request_id, "approved", status_only=False))


# The audit log's db_role


def test_the_database_records_who_wrote_each_audit_row(pg: ControlDatabase) -> None:
    assert pg.requester_raw is not None
    pg.requester_raw(
        "INSERT INTO agent_core_audit (seq, schema_version, event_id, occurred_at, action, "
        "actor_id, payload, prev_hash, record_hash, db_role) VALUES (1, 3, "
        f"'{uuid4()}', '{stamp(NOW)}', 'approval.resolved', 'user-17', '{{}}', '{'0' * 64}', "
        f"'{'0' * 64}', '{APPROVER_ROLE}')"
    )

    assert pg.raw("SELECT actor_id, db_role FROM agent_core_audit") == [("user-17", REQUESTER_ROLE)]
    assert refused(pg.superuser_raw, f"UPDATE agent_core_audit SET db_role = '{APPROVER_ROLE}'")


# The installer


def catalog(database: ControlDatabase, schema: str = "public") -> list[Any]:
    """Everything the installer defines in `schema`, in a comparable form."""
    tables = "('agent_core_audit', 'agent_core_approvals', 'agent_core_approval_roles')"
    return [
        database.superuser_raw(
            "SELECT c.relname, c.relacl::text, c.relowner FROM pg_class c "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '{schema}' "
            f"AND c.relname IN {tables} ORDER BY 1"
        ),
        database.superuser_raw(
            "SELECT c.relname, a.attname, a.attacl::text, format_type(a.atttypid, a.atttypmod), "
            "a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '{schema}' "
            f"AND c.relname IN {tables} AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2"
        ),
        database.superuser_raw(
            "SELECT t.tgname, pg_get_triggerdef(t.oid), t.tgenabled FROM pg_trigger t "
            f"JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{schema}' AND NOT t.tgisinternal ORDER BY 1"
        ),
        database.superuser_raw(
            "SELECT p.proname, pg_get_functiondef(p.oid), p.proacl::text FROM pg_proc p "
            f"JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = '{schema}' "
            "AND p.proname LIKE 'agent_core%' ORDER BY 1"
        ),
        database.superuser_raw(f"SELECT * FROM {schema}.agent_core_approval_roles"),
        database.superuser_raw(f"SELECT indexdef FROM pg_indexes WHERE schemaname = '{schema}'"),
    ]


def test_installing_twice_changes_nothing(pg: ControlDatabase) -> None:
    assert pg.owner_url is not None
    before = catalog(pg)

    report = install_postgres_schema(
        pg.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
    )

    assert catalog(pg) == before
    assert report.outside_layout == ()


def test_installing_with_other_roles_is_refused_and_changes_nothing(pg: ControlDatabase) -> None:
    assert pg.owner_url is not None
    before = catalog(pg)

    with pytest.raises(ConfigError, match="will not add others"):
        install_postgres_schema(
            pg.owner_url, requester_role=LEGACY_APP_ROLE, approver_role=APPROVER_ROLE
        )

    assert catalog(pg) == before


def test_overlapping_roles_are_refused(pg: ControlDatabase) -> None:
    assert pg.owner_url is not None
    pg.login_role(f"GRANT {REQUESTER_ROLE} TO {{role}}")
    member_of_requester = pg.roles[-1]

    with pytest.raises(ConfigError, match="overlap"):
        install_postgres_schema(
            pg.owner_url, requester_role=REQUESTER_ROLE, approver_role=member_of_requester
        )
    with pytest.raises(ConfigError, match="different roles"):
        install_postgres_schema(
            pg.owner_url, requester_role=REQUESTER_ROLE, approver_role=REQUESTER_ROLE
        )


def test_a_rerun_never_regrants_what_an_operator_tightened(pg: ControlDatabase) -> None:
    assert pg.owner_url is not None
    pg.raw(f"REVOKE UPDATE (reason) ON agent_core_approvals FROM {APPROVER_ROLE}")
    before = catalog(pg)

    install_postgres_schema(
        pg.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
    )

    assert catalog(pg) == before


def test_the_report_lists_grants_outside_the_layout(pg: ControlDatabase) -> None:
    assert pg.owner_url is not None
    pg.raw(f"GRANT UPDATE ON agent_core_approvals TO {LEGACY_APP_ROLE}")
    pg.raw(f"GRANT DELETE ON agent_core_audit TO {REQUESTER_ROLE}")

    report = install_postgres_schema(
        pg.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
    )

    assert set(report.outside_layout) == {
        Grant(table="agent_core_approvals", role=LEGACY_APP_ROLE, privilege="UPDATE"),
        Grant(table="agent_core_audit", role=REQUESTER_ROLE, privilege="DELETE"),
    }
    assert f"{LEGACY_APP_ROLE} has UPDATE on agent_core_approvals" in str(report)


def test_a_non_default_schema_holds_everything() -> None:
    with postgres_database(schema="tenant_a") as database:
        assert database.raw(
            "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'agent_core%' "
            "AND schemaname = 'public'"
        ) == [(0,)]
        assert len(catalog(database, "tenant_a")[0]) == 3
        request_id = planted(database, "pending")
        assert database.approver_raw is not None
        database.approver_raw(
            "UPDATE tenant_a.agent_core_approvals SET status = 'approved', decision = 'approve', "
            f"resolved_by = 'user-17', resolved_at = '{stamp(NOW)}' WHERE id = '{request_id}'"
        )
        assert status_of(database, request_id) == "approved"


def test_tables_from_0_1_0a1_are_refused() -> None:
    with postgres_database(install=False) as database:
        assert database.owner_url is not None
        database.raw("CREATE TABLE agent_core_audit (seq BIGINT PRIMARY KEY)")

        with pytest.raises(ConfigError, match=r"0\.1\.0a1"):
            install_postgres_schema(
                database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
            )


def a2_audit_record() -> AuditRecord:
    unsealed = UnsealedAuditRecord(
        schema_version=2,
        seq=1,
        event_id=uuid4(),
        occurred_at=NOW,
        action="model.call",
        actor_id="svc-triage",
        subject_id=None,
        payload={},
        run_context=None,
        prev_hash=GENESIS_HASH,
    )
    return AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))


def load_a2_schema(database: ControlDatabase) -> None:
    for statement in A2_SCHEMA.read_text().split(";\n\n"):
        body = "\n".join(
            line for line in statement.splitlines() if not line.startswith("--")
        ).strip()
        if body:
            database.raw(body.replace("APP_ROLE", LEGACY_APP_ROLE))


async def test_an_a2_schema_is_upgraded_in_place() -> None:
    with postgres_database(install=False) as database:
        assert database.owner_url is not None
        assert database.legacy_raw is not None
        load_a2_schema(database)
        legacy_id = str(uuid4())
        # What a2 allowed: the app role writes an approved row with plain SQL.
        database.legacy_raw(
            "INSERT INTO agent_core_approvals (id, action, summary, payload_sha256, requested_by, "
            "required_role, created_at, expires_at, status, decision, resolved_by, resolved_at) "
            f"VALUES ('{legacy_id}', 'crm.update_contact', 's', '{'a' * 64}', 'agent-intake', "
            f"'ops.approver', '{stamp(NOW)}', '{stamp(NOW + timedelta(hours=1))}', 'approved', "
            f"'approve', 'user-17', '{stamp(NOW)}')"
        )

        legacy_record = a2_audit_record()
        database.legacy_raw(
            "INSERT INTO agent_core_audit (seq, schema_version, event_id, occurred_at, action, "
            "actor_id, subject_id, payload, run_context, prev_hash, record_hash) VALUES "
            f"(1, 2, '{legacy_record.event_id}', '{stamp(legacy_record.occurred_at)}', "
            f"'model.call', 'svc-triage', NULL, '{{}}', NULL, '{GENESIS_HASH}', "
            f"'{legacy_record.record_hash}')"
        )

        report = install_postgres_schema(
            database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
        )

        # The a2 record and a new one share one chain, each at its own version.
        log = SQLAuditLog(database.database)
        await log.append(AuditEvent(action="model.call", actor_id="svc-triage"))
        records = [record async for record in log.iter_records()]
        assert [(record.schema_version, record.db_role) for record in records] == [
            (2, None),
            (3, REQUESTER_ROLE),
        ]
        assert (await log.verify()).seq == 2
        assert status_of(database, legacy_id) == "approved"
        assert report.unaudited_approvals == (legacy_id,)
        assert legacy_id in str(report)
        assert {grant.role for grant in report.outside_layout} == {LEGACY_APP_ROLE}
        assert (
            Grant(table="agent_core_approvals", role=LEGACY_APP_ROLE, privilege="UPDATE")
            in report.outside_layout
        )
        # The legacy role keeps its grants, but the guard now refuses its writes.
        pending_id = planted(database, "pending")
        assert refused(
            database.legacy_raw, transition_sql(pending_id, "approved", status_only=False)
        )
        assert database.approver_raw is not None
        database.approver_raw(transition_sql(pending_id, "approved", status_only=False))
        assert status_of(database, pending_id) == "approved"
        assert (
            install_postgres_schema(
                database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
            ).outside_layout
            == report.outside_layout
        )

        # Asked to, a run cancels what no approver-side audit event approved: the a2
        # row, even after the requester forges an event for it, and the row approved
        # above by plain SQL outside the library. A library approval stays.
        queue = split_queue(database)
        proper = await queue.submit(
            action="crm.update_contact",
            summary="s",
            payload={},
            requested_by=Principal(id="agent-intake", kind=PrincipalKind.AGENT),
            required_role="ops.approver",
            ttl_seconds=600,
        )
        await queue.resolve(
            proper.id,
            decision=Decision.APPROVE,
            principal=Principal(
                id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"})
            ),
        )
        assert database.requester_raw is not None
        next_seq, head_hash = database.raw(
            "SELECT seq + 1, record_hash FROM agent_core_audit ORDER BY seq DESC LIMIT 1"
        )[0]
        database.requester_raw(
            "INSERT INTO agent_core_audit (seq, schema_version, event_id, occurred_at, action, "
            "actor_id, subject_id, payload, prev_hash, record_hash) VALUES "
            f"({next_seq}, 3, '{uuid4()}', '{stamp(NOW)}', 'approval.resolved', 'user-17', "
            f'\'{legacy_id}\', \'{{"approval_action":"crm.update_contact","decision":'
            f"\"approve\"}}', '{head_hash}', '{'0' * 64}')"
        )

        closing = install_postgres_schema(
            database.owner_url,
            requester_role=REQUESTER_ROLE,
            approver_role=APPROVER_ROLE,
            close_unaudited_approvals=True,
        )

        assert set(closing.closed_approvals) == {legacy_id, pending_id}
        assert status_of(database, legacy_id) == "cancelled"
        assert status_of(database, str(proper.id)) == "approved"
        assert database.raw(
            f"SELECT reason FROM agent_core_approvals WHERE id = '{legacy_id}'"
        ) == [
            (
                f"Cancelled by install_postgres_schema: approved by user-17 at {stamp(NOW)}, "
                "with no approval.resolved audit event from the approver side.",
            )
        ]
        assert database.raw(
            "SELECT tgenabled FROM pg_trigger WHERE tgname = 'agent_core_approvals_guard'"
        ) == [("O",)]
        assert (
            install_postgres_schema(
                database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
            ).unaudited_approvals
            == ()
        )


# Row shapes: the guard refuses what the library could not read back


def insert_sql(**overrides: str) -> str:
    columns = {
        "id": f"'{uuid4()}'",
        "action": "'crm.update_contact'",
        "summary": "'s'",
        "payload_sha256": f"'{'a' * 64}'",
        "requested_by": "'agent-intake'",
        "required_role": "'ops.approver'",
        "created_at": f"'{stamp(NOW)}'",
        "expires_at": f"'{stamp(NOW + timedelta(hours=1))}'",
        "status": "'pending'",
        **overrides,
    }
    return (
        f"INSERT INTO agent_core_approvals ({', '.join(columns)}) "
        f"VALUES ({', '.join(columns.values())})"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"created_at": f"'{NOW:%Y-%m-%d %H:%M:%S}'"},
        {"expires_at": "'infinity'"},
        {"id": "'not-a-uuid'"},
        {"action": "'Not An Action'"},
        {"summary": "''"},
        {"payload_sha256": "'ABC'"},
        {"requested_by": "'jane@example.com'"},
        {"required_role": "'Ops Approver'"},
        {"delegates": "'{\"a\": 1}'"},
        {"delegates": "'[\"has space\"]'"},
        {"delegates": "'[" + ", ".join(f'"svc-{n}"' for n in range(17)) + "]'"},
        {"run_context": '\'{"run_id": "r1", "extra": 1}\''},
        {"run_context": '\'{"run_id": "r1", "external_ids": {"Bad": "x"}}\''},
        {"run_context": "'[1]'"},
        {"expires_at": f"'{(NOW + timedelta(days=1)):%Y-%m-%d}T24:00:00.000000Z'"},
        {"expires_at": f"'{(NOW + timedelta(days=1)):%Y-%m-%d}T23:59:60.000000Z'"},
        {"expires_at": f"'{NOW.year + 1}-02-30T00:00:00.000000Z'"},
    ],
    ids=[
        "created-at-local-time",
        "expires-at-infinity",
        "id",
        "action",
        "empty-summary",
        "payload-hash",
        "requested-by-email",
        "required-role",
        "delegates-object",
        "delegate-shape",
        "too-many-delegates",
        "run-context-extra-key",
        "run-context-id-name",
        "run-context-array",
        "hour-24",
        "leap-second",
        "february-30",
    ],
)
def test_the_guard_refuses_rows_of_the_wrong_shape(
    pg: ControlDatabase, overrides: dict[str, str]
) -> None:
    assert pg.requester_raw is not None

    assert refused(pg.requester_raw, insert_sql(**overrides))
    assert not refused(pg.requester_raw, insert_sql())


def test_a_session_time_zone_cannot_stretch_a_lifetime(pg: ControlDatabase) -> None:
    # Without an offset, a timestamp would be read in the session's time zone.
    assert pg.requester_raw is not None
    local = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")

    assert refused(
        pg.requester_raw,
        "SET TimeZone = 'Pacific/Kiritimati'; " + insert_sql(expires_at=f"'{local}'"),
    )


@pytest.mark.parametrize(
    "assignments",
    [
        "resolved_at = '2026-10-02 12:00:00'",
        "resolved_by = 'jane@example.com', resolved_at = '{now}'",
        "reason = '', resolved_at = '{now}'",
        "resolved_at = '{day}T24:00:00.000000Z'",
    ],
    ids=["resolved-at", "resolved-by", "empty-reason", "resolved-at-hour-24"],
)
def test_a_decision_of_the_wrong_shape_is_refused(pg: ControlDatabase, assignments: str) -> None:
    assert pg.approver_raw is not None
    request_id = planted(pg, "pending")
    sql = (
        "UPDATE agent_core_approvals SET status = 'approved', decision = 'approve', "
        + ("resolved_by = 'user-17', " if "resolved_by" not in assignments else "")
        + assignments.format(now=stamp(NOW), day=f"{NOW:%Y-%m-%d}")
        + f" WHERE id = '{request_id}'"
    )

    assert refused(pg.approver_raw, sql)
    assert status_of(pg, request_id) == "pending"


def test_a_row_with_an_overlong_lifetime_can_be_neither_decided_nor_used(
    pg: ControlDatabase,
) -> None:
    # What a2 let any app role write: a ten-year lifetime, past the guard.
    approved = planted(pg, "approved")
    pending = planted(pg, "pending")
    pg.superuser_raw(
        "SET session_replication_role = replica; UPDATE agent_core_approvals "
        f"SET expires_at = '{stamp(NOW + timedelta(days=3650))}'"
    )
    assert pg.requester_raw is not None
    assert pg.approver_raw is not None

    assert refused(pg.requester_raw, transition_sql(approved, "consumed", status_only=False))
    assert refused(pg.approver_raw, transition_sql(pending, "approved", status_only=False))
    assert not refused(pg.requester_raw, transition_sql(pending, "cancelled", status_only=False))


@pytest.mark.parametrize("forged", [False, True], ids=["no-events", "requester-forged-event"])
def test_closing_is_refused_when_the_audit_log_lives_elsewhere(
    pg: ControlDatabase, forged: bool
) -> None:
    assert pg.owner_url is not None
    assert pg.requester_raw is not None
    request_id = planted(pg, "approved")  # approved, and no approver-side event here
    if forged:
        # One event the requester appends must not make the local log look in use.
        pg.requester_raw(
            "INSERT INTO agent_core_audit (seq, schema_version, event_id, occurred_at, action, "
            "actor_id, subject_id, payload, prev_hash, record_hash) VALUES "
            f"(1, 3, '{uuid4()}', '{stamp(NOW)}', 'approval.resolved', 'user-17', "
            f"'{uuid4()}', '{{}}', '{'0' * 64}', '{'0' * 64}')"
        )

    with pytest.raises(ConfigError, match="audit log may live elsewhere"):
        install_postgres_schema(
            pg.owner_url,
            requester_role=REQUESTER_ROLE,
            approver_role=APPROVER_ROLE,
            close_unaudited_approvals=True,
        )

    assert status_of(pg, request_id) == "approved"


def test_the_installer_lists_finished_requests_whose_finish_time_a_client_backdated(
    control_database: ControlDatabase,
) -> None:
    """0.1.0a4 let the closing role write closed_at and consumed_at: such a row is purgeable
    at once under any floor. The report names them, only those still holding a payload."""
    if control_database.owner_url is None or control_database.superuser_url is None:
        pytest.skip("the installer is Postgres only")
    now = datetime.now(UTC)

    def stamp(moment: datetime) -> str:
        return "'" + moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ") + "'"

    def plant(**columns: str) -> str:
        row_id = str(uuid4())
        control_database.superuser_raw(
            "SET session_replication_role = replica; "
            + raw_request(
                **{
                    "id": f"'{row_id}'",
                    "created_at": stamp(now - timedelta(days=2)),
                    "expires_at": stamp(now - timedelta(days=1)),
                    "payload_json": "'{\"a\": 1}'",
                    **columns,
                }
            )
        )
        return row_id

    decided = {
        "decision": "'approve'",
        "resolved_by": "'user-17'",
        "resolved_at": stamp(now - timedelta(days=1, hours=12)),
    }
    cancelled_before_created = plant(status="'cancelled'", closed_at=stamp(now - timedelta(days=9)))
    consumed_before_approved = plant(
        status="'consumed'", consumed_at=stamp(now - timedelta(days=1, hours=20)), **decided
    )
    honest = plant(status="'cancelled'", closed_at=stamp(now - timedelta(hours=3)))
    # Clock skew the guard allows (5 minutes) is not tampering: the stamp is the database's.
    skewed_requester = plant(
        status="'cancelled'",
        created_at=stamp(now + timedelta(minutes=3)),
        expires_at=stamp(now + timedelta(hours=1)),
        closed_at=stamp(now),
    )
    skewed_approver = plant(
        status="'consumed'",
        consumed_at=stamp(now),
        **{**decided, "resolved_at": stamp(now + timedelta(minutes=4))},
    )
    purged = plant(
        status="'cancelled'",
        closed_at=stamp(now - timedelta(days=9)),
        payload_json="NULL",
        payload_purged_at=stamp(now),
    )
    still_open = plant(status="'pending'")
    # Values past the shape pattern that no timestamp parser accepts: the report must not fail.
    impossible_day = plant(status="'cancelled'", closed_at="'2026-02-30T00:00:00.000000Z'")
    impossible_time = plant(status="'cancelled'", closed_at="'2026-01-01T25:61:61.000000Z'")
    impossible_created = plant(
        status="'cancelled'",
        created_at="'2026-13-01T00:00:00.000000Z'",
        closed_at=stamp(now),
    )

    report = install_postgres_schema(
        control_database.owner_url,
        requester_role=REQUESTER_ROLE,
        approver_role=APPROVER_ROLE,
        schema=control_database.schema,
    )

    assert set(report.backdated_finishes) == {cancelled_before_created, consumed_before_approved}
    assert {
        honest,
        skewed_requester,
        skewed_approver,
        purged,
        still_open,
        impossible_day,
        impossible_time,
        impossible_created,
    }.isdisjoint(report.backdated_finishes)
    assert cancelled_before_created in str(report)
    again = install_postgres_schema(
        control_database.owner_url,
        requester_role=REQUESTER_ROLE,
        approver_role=APPROVER_ROLE,
        schema=control_database.schema,
    )
    assert again.backdated_finishes == report.backdated_finishes


async def test_an_approvals_guard_of_revision_5_is_refused_until_the_installer_is_run_again(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("the guard is Postgres only")
    schema = control_database.schema
    definition = control_database.superuser_raw(
        f"SELECT pg_get_functiondef('{schema}.agent_core_approvals_guard()'::regprocedure)"
    )[0][0]
    assert "-- agent-core guard revision 6" in definition
    control_database.superuser_raw(
        definition.replace("-- agent-core guard revision 6", "-- agent-core guard revision 5")
    )

    queue = split_queue(control_database)
    with pytest.raises(ConfigError, match=r"revision 5; this release needs 6"):
        await queue.submit(
            action="crm.update_contact",
            summary="s",
            payload={"a": 1},
            requested_by=REQUESTER,
            required_role="ops.approver",
            ttl_seconds=60,
        )
    with pytest.raises(ConfigError, match=r"revision 5; this release needs 6"):
        await queue.approver.purge_payloads(
            principal=Principal(id="svc-retention", kind=PrincipalKind.SERVICE),
            older_than=timedelta(days=2),
        )
