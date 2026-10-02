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

from aox_agent_core.storage import Database, install_postgres_schema, open_database

ADMIN_URL_ENV = "AGENT_CORE_TEST_POSTGRES_ADMIN_URL"
REQUIRE_ENV = "AGENT_CORE_REQUIRE_POSTGRES"
OWNER_ROLE = "agent_core_owner"
APP_ROLE = "agent_core_app"

_CREATE_ROLES = f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{OWNER_ROLE}') THEN
        CREATE ROLE {OWNER_ROLE} LOGIN NOSUPERUSER CREATEDB;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$"""


@dataclass
class ControlDatabase:
    """The library's view (app role) plus raw access for tampering (owner/superuser)."""

    backend: str
    database: Database
    url: str
    raw: Callable[[str], list[tuple[Any, ...]]]
    superuser_raw: Callable[[str], list[tuple[Any, ...]]]
    owner_url: str | None = None
    superuser_url: str | None = None
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


def sqlite_database(tmp_path: Path) -> ControlDatabase:
    path = tmp_path / "control.sqlite3"

    def raw(sql: str) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(path)) as connection, connection:
            return connection.execute(sql).fetchall()

    url = f"sqlite:///{path}"
    return ControlDatabase("sqlite", open_database(url), url, raw, raw)


@contextmanager
def postgres_database() -> Iterator[ControlDatabase]:
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
    app_url = _with(admin_url, user=APP_ROLE, database=name)
    superuser_url = _with(admin_url, database=name)
    install_postgres_schema(owner_url, app_role=APP_ROLE)

    def runner(url: str) -> Callable[[str], list[tuple[Any, ...]]]:
        def raw(sql: str) -> list[tuple[Any, ...]]:
            with psycopg.connect(url, autocommit=True) as connection:
                cursor = connection.execute(sql)
                return list(cursor.fetchall()) if cursor.description else []

        return raw

    database = ControlDatabase(
        "postgres",
        open_database(app_url),
        app_url,
        runner(owner_url),
        runner(superuser_url),
        owner_url=owner_url,
        superuser_url=superuser_url,
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
