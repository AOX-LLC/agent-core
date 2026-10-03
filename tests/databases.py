"""Test databases: a SQLite file, or a fresh Postgres database with owner and app roles.

Postgres tests need AGENT_CORE_TEST_POSTGRES_ADMIN_URL (a superuser URL, e.g. the
Compose service on 127.0.0.1:4202). Without it they are skipped, unless
AGENT_CORE_REQUIRE_POSTGRES=1, which CI sets, makes that a failure.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest

from aox_agent_core.approvals import RoleApproverPolicy
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.replay.scrub import Scrubber
from aox_agent_core.storage import Database, install_postgres_schema, open_database

ADMIN_URL_ENV = "AGENT_CORE_TEST_POSTGRES_ADMIN_URL"
REQUIRE_ENV = "AGENT_CORE_REQUIRE_POSTGRES"
OWNER_ROLE = "agent_core_owner"
REQUESTER_ROLE = "agent_core_requester"
APPROVER_ROLE = "agent_core_approver"
# The role each action in the tests needs, as an approver side would configure it.
TEST_ACTION_ROLES = {"crm.update_contact": "ops.approver", "crm.delete_contact": "ops.approver"}
# The single application role of 0.1.0a2, for the upgrade tests.
LEGACY_APP_ROLE = "agent_core_app"

_CREATE_ROLES = f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{OWNER_ROLE}') THEN
        CREATE ROLE {OWNER_ROLE} LOGIN NOSUPERUSER CREATEDB;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{REQUESTER_ROLE}') THEN
        CREATE ROLE {REQUESTER_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APPROVER_ROLE}') THEN
        CREATE ROLE {APPROVER_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{LEGACY_APP_ROLE}') THEN
        CREATE ROLE {LEGACY_APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$"""


Raw = Callable[[str], list[tuple[Any, ...]]]


@dataclass
class ControlDatabase:
    """The library's two views plus raw access for tampering (owner/superuser).

    `database` connects as the requester role and `approver_database` as the
    approver role. SQLite has no roles: both are the same file.
    """

    backend: str
    database: Database
    url: str
    raw: Raw
    superuser_raw: Raw
    approver_database: Database
    owner_url: str | None = None
    superuser_url: str | None = None
    requester_raw: Raw | None = None
    approver_raw: Raw | None = None
    legacy_raw: Raw | None = None
    schema: str = "public"
    roles: list[str] = field(default_factory=list)

    def login_role(self, *statements: str) -> str:
        """Create a NOINHERIT login role, run `statements` ({role} is its name), return its URL.

        The fixture drops the role after the test database is gone.
        """
        assert self.superuser_url is not None, "roles exist only on Postgres"
        role = f"agent_core_probe_{uuid4().hex[:8]}"
        self.superuser_raw(f"CREATE ROLE {role} LOGIN NOINHERIT")
        self.roles.append(role)
        for statement in statements:
            self.superuser_raw(statement.format(role=role))
        return _with(self.superuser_url, user=role)


def approver_login(
    database: ControlDatabase, *, also_member_of: tuple[str, ...] = ()
) -> tuple[str, str]:
    """A login role that is a member of the approver role (and of `also_member_of`), with its URL.

    It inherits, so it holds the approver role's privileges as itself. The fixture drops it.
    """
    assert database.superuser_url is not None, "roles exist only on Postgres"
    role = f"agent_core_login_{uuid4().hex[:8]}"
    database.superuser_raw(f"CREATE ROLE {role} LOGIN INHERIT")
    database.roles.append(role)
    for member_of in (APPROVER_ROLE, *also_member_of):
        database.superuser_raw(f"GRANT {member_of} TO {role}")
    return role, _with(database.superuser_url, user=role)


def sqlite_database(tmp_path: Path) -> ControlDatabase:
    path = tmp_path / "control.sqlite3"

    def raw(sql: str) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(path)) as connection, connection:
            return connection.execute(sql).fetchall()

    url = f"sqlite:///{path}"
    database = open_database(url)
    return ControlDatabase("sqlite", database, url, raw, raw, approver_database=database)


@contextmanager
def postgres_database(*, schema: str = "public", install: bool = True) -> Iterator[ControlDatabase]:
    """A fresh database with the library installed in `schema` (or nothing installed)."""
    admin_url = os.environ.get(ADMIN_URL_ENV)
    if not admin_url:
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(f"{REQUIRE_ENV}=1 but {ADMIN_URL_ENV} is not set")
        pytest.skip(f"set {ADMIN_URL_ENV} to run Postgres tests")

    name = f"agent_core_test_{uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(_CREATE_ROLES)
        admin.execute(f'CREATE DATABASE "{name}" OWNER {OWNER_ROLE}')
    owner_url = _with(admin_url, user=OWNER_ROLE, database=name)
    requester_url = _with(admin_url, user=REQUESTER_ROLE, database=name)
    approver_url = _with(admin_url, user=APPROVER_ROLE, database=name)
    superuser_url = _with(admin_url, database=name)
    if install:
        install_postgres_schema(
            owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE, schema=schema
        )

    def runner(url: str) -> Callable[[str], list[tuple[Any, ...]]]:
        def raw(sql: str) -> list[tuple[Any, ...]]:
            with psycopg.connect(url, autocommit=True) as connection:
                cursor = connection.execute(sql)
                return list(cursor.fetchall()) if cursor.description else []

        return raw

    database = ControlDatabase(
        "postgres",
        open_database(requester_url),
        requester_url,
        runner(owner_url),
        runner(superuser_url),
        approver_database=open_database(approver_url),
        owner_url=owner_url,
        superuser_url=superuser_url,
        requester_raw=runner(requester_url),
        approver_raw=runner(approver_url),
        legacy_raw=runner(_with(admin_url, user=LEGACY_APP_ROLE, database=name)),
        schema=schema,
    )
    try:
        yield database
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            for role in database.roles:
                admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def _with(url: str, *, user: str | None = None, database: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rpartition("@")[2]
        netloc = f"{user}@{host}"
    path = f"/{database}" if database is not None else parts.path
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


class SplitQueue:
    """An approval queue as a deployment runs it, for tests on both backends.

    Submitting, consuming, cancelling and reading go through the requester's
    connection; deciding and listing what to decide, through the approver's. Each
    side audits to a log on its own connection. On SQLite both are one file.
    """

    def __init__(self, requester: SQLApprovalQueue, approver: SQLApprovalQueue) -> None:
        self.requester = requester
        self.approver = approver
        self.submit = requester.submit
        self.get = requester.get
        self.consume = requester.consume
        self.cancel = requester.cancel
        self.expire_due = requester.expire_due
        self.resolve = approver.resolve
        self.list_pending = approver.list_pending


def split_queue(
    database: ControlDatabase, *, scrubber: Scrubber | None = None, **options: Any
) -> SplitQueue:
    """Both sides of the approval queue on `database`, built with the same options."""

    schema = database.schema if database.backend == "postgres" else None
    options.setdefault("policy", RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES))

    def side(connection: Database) -> SQLApprovalQueue:
        log = SQLAuditLog(connection, scrubber=scrubber, schema=schema)
        return SQLApprovalQueue(connection, audit_log=log, schema=schema, **options)

    return SplitQueue(side(database.database), side(database.approver_database))
