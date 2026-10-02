"""SQL storage shared by the audit log and the approval queue.

Both backends use synchronous drivers on a worker thread, so one code path serves
SQLite (standard library) and Postgres (the `postgres` extra, psycopg 3). Every
operation runs in its own short transaction on its own connection; that is plenty
for audit and approval volumes and keeps connection state out of the picture.

When the audit log and the approval queue share a Database, the queue writes its
audit events in the same transaction as the change they describe.
"""

import asyncio
import re
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pydantic import SecretStr

from aox_agent_core.errors import ConfigError

ResultT = TypeVar("ResultT")

SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
POSTGRES_DEFAULT_PORT = 5432
POSTGRES_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class Dialect(StrEnum):
    """The SQL dialects the library supports."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


class Session:
    """One open transaction. SQL is written with '?' placeholders for both dialects."""

    def __init__(self, cursor: Any, dialect: Dialect) -> None:
        self._cursor = cursor
        self.dialect = dialect

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run one statement and return its rows (empty for statements without rows)."""
        self._cursor.execute(self._native(sql), tuple(parameters))
        if self._cursor.description is None:
            return []
        return [tuple(row) for row in self._cursor.fetchall()]

    def execute_count(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """Run one statement and return how many rows it changed."""
        self._cursor.execute(self._native(sql), tuple(parameters))
        return int(self._cursor.rowcount)

    def _native(self, sql: str) -> str:
        # Plain replace: the library's SQL never has a literal '?' inside a string.
        return sql.replace("?", "%s") if self.dialect is Dialect.POSTGRES else sql


class Database(ABC):
    """A database the library's tables live in."""

    dialect: Dialect

    async def run(self, work: Callable[[Session], ResultT], *, write: bool = False) -> ResultT:
        """Run `work` in one transaction on a worker thread and return its result.

        On SQLite a write transaction takes the database's write lock up front (BEGIN
        IMMEDIATE), so concurrent writers queue instead of failing later. Postgres
        has no equivalent here; writers that must be serialized take their own lock
        (the audit log uses an advisory lock).
        """
        return await asyncio.to_thread(self.run_sync, work, write=write)

    def same_database(self, other: "Database") -> bool:
        """True when both point at the same database, even as separate objects."""
        return self._identity() == other._identity()

    @abstractmethod
    def _identity(self) -> tuple[str, ...]: ...

    def run_sync(self, work: Callable[[Session], ResultT], *, write: bool = False) -> ResultT:
        """Blocking form of run(). Commits if `work` returns, rolls back if it raises."""
        with self._transaction(write=write) as session:
            return work(session)

    @abstractmethod
    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[Session]: ...


class SQLiteDatabase(Database):
    """A SQLite file. The file is created on first use."""

    dialect = Dialect.SQLITE

    def __init__(self, path: Path) -> None:
        self.path = path

    def _identity(self) -> tuple[str, ...]:
        return (self.dialect.value, str(self.path.resolve()))

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[Session]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS, isolation_level=None
        )
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield Session(connection.cursor(), self.dialect)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()


class PostgresDatabase(Database):
    """A Postgres database, reached with the `postgres` extra (psycopg 3)."""

    dialect = Dialect.POSTGRES

    def __init__(self, url: SecretStr) -> None:
        self._url = url
        self._psycopg = _import_psycopg()

    def _identity(self) -> tuple[str, ...]:
        parts = urlsplit(self._url.get_secret_value())
        host = (parts.hostname or "localhost").lower()
        port = str(parts.port or POSTGRES_DEFAULT_PORT)
        return (self.dialect.value, host, port, parts.path.lstrip("/"))

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[Session]:
        with self._psycopg.connect(self._url.get_secret_value(), autocommit=False) as connection:
            # Appends read the head and insert after it under an advisory lock; that
            # is only safe in READ COMMITTED, whatever the server's default is.
            connection.isolation_level = self._psycopg.IsolationLevel.READ_COMMITTED
            with connection.transaction(), connection.cursor() as cursor:
                yield Session(cursor, self.dialect)


def open_database(url: str | SecretStr) -> Database:
    """Open a database from a URL: sqlite:///relative/path, sqlite:////absolute/path,
    or postgresql://user@host:port/name.

    A Postgres URL may carry a password, so it is kept as a SecretStr.
    """
    secret_url = url if isinstance(url, SecretStr) else SecretStr(url)
    text = secret_url.get_secret_value()
    scheme = urlsplit(text).scheme
    if scheme == "sqlite":
        path = text.removeprefix("sqlite:///")
        if not path or path == text:
            raise ConfigError("A SQLite URL looks like sqlite:///audit.sqlite3.")
        return SQLiteDatabase(Path(path))
    if scheme in {"postgresql", "postgres"}:
        return PostgresDatabase(secret_url)
    raise ConfigError(f"Unsupported database URL scheme {scheme!r}; use sqlite or postgresql.")


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as error:
        raise ConfigError(
            "Postgres needs the postgres extra: pip install 'aox-agent-core[postgres]'."
        ) from error
    return psycopg


def install_postgres_schema(owner_url: str | SecretStr, *, app_role: str) -> None:
    """Create the audit and approval tables, their triggers and grants, as the owner role.

    Run once per database by an operator, never by the application. The owner
    keeps every privilege; `app_role` may only INSERT and SELECT on the audit
    table, and SELECT, INSERT and UPDATE on the approvals table.
    """
    from aox_agent_core.approvals import sql as approvals_sql
    from aox_agent_core.audit import sql as audit_sql

    if not POSTGRES_ROLE_NAME.fullmatch(app_role):
        raise ConfigError(f"{app_role!r} is not a plain Postgres role name.")
    database = open_database(owner_url)
    if database.dialect is not Dialect.POSTGRES:
        raise ConfigError("install_postgres_schema needs a postgresql:// URL.")
    statements = (
        *audit_sql.POSTGRES_SCHEMA,
        *audit_sql.postgres_grants(app_role),
        *approvals_sql.SCHEMA,
        *approvals_sql.postgres_grants(app_role),
    )

    def install(session: Session) -> None:
        for statement in statements:
            session.execute(statement)

    database.run_sync(install, write=True)


__all__ = [
    "Database",
    "Dialect",
    "PostgresDatabase",
    "SQLiteDatabase",
    "Session",
    "install_postgres_schema",
    "open_database",
]
