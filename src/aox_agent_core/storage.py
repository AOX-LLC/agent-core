"""SQL storage shared by the audit log and the approval queue.

Both backends use synchronous drivers on a worker thread, so one code path serves
SQLite (standard library) and Postgres (the `postgres` extra, psycopg 3). Every
operation runs in its own short transaction on its own connection; that is plenty
for audit and approval volumes and keeps connection state out of the picture.

When the audit log and the approval queue share a Database, the queue writes its
audit events in the same transaction as the change they describe.
"""

import asyncio
import os
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
# Row-value comparisons, which approval listing uses, arrived in SQLite 3.15.
SQLITE_MINIMUM_VERSION = (3, 15, 0)
POSTGRES_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


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
        self._run(sql, parameters)
        if self._cursor.description is None:
            return []
        return [tuple(row) for row in self._cursor.fetchall()]

    def execute_count(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """Run one statement and return how many rows it changed."""
        self._run(sql, parameters)
        return int(self._cursor.rowcount)

    def _run(self, sql: str, parameters: Sequence[Any]) -> None:
        if self.dialect is Dialect.SQLITE:
            self._cursor.execute(sql, tuple(parameters))
        elif parameters:
            # psycopg uses %s placeholders, so a literal % must be doubled.
            self._cursor.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(parameters))
        else:
            # Without parameters psycopg sends the text as it is.
            self._cursor.execute(sql)


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
    """A SQLite file. The file is created by the first write, never by a read."""

    dialect = Dialect.SQLITE

    def __init__(self, path: Path) -> None:
        if sqlite3.sqlite_version_info < SQLITE_MINIMUM_VERSION:
            raise ConfigError(
                f"SQLite {sqlite3.sqlite_version} is too old; the library needs "
                f"{'.'.join(map(str, SQLITE_MINIMUM_VERSION))} or later."
            )
        self.path = path

    def _identity(self) -> tuple[str, ...]:
        return (self.dialect.value, str(self.path.resolve()))

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[Session]:
        if not write and not self.path.exists():
            # A file that does not exist reads as an empty, read-only database:
            # reading never creates it, so a mistyped path is not silently turned
            # into a new log, and a write sent as a read fails instead of vanishing.
            connection = sqlite3.connect("file::memory:?mode=ro", uri=True, isolation_level=None)
        else:
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
        # Every connection setting but the password: two URLs that differ in user,
        # socket directory, options or search_path may reach different databases,
        # schemas or privileges, so they are not treated as the same database.
        settings = self._psycopg.conninfo.conninfo_to_dict(self._url.get_secret_value())
        settings.pop("password", None)
        # Spellings of the same address are one database: TCP host names are case
        # insensitive, and a TCP host without a port means libpq's default port.
        # Unix socket directories are paths, so their case is kept.
        host = str(settings.get("host", ""))
        is_tcp_host = bool(host) and not host.startswith(("/", "@"))
        if is_tcp_host:
            settings["host"] = host.lower()
            default_port = os.environ.get("PGPORT", str(POSTGRES_DEFAULT_PORT))
            settings.setdefault("port", default_port)
        return (self.dialect.value, *(f"{key}={value}" for key, value in sorted(settings.items())))

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
        if path == ":memory:" or path.startswith("file::memory"):
            # Each operation opens its own connection, so an in-memory database
            # would vanish between them.
            raise ConfigError("An in-memory SQLite database cannot hold the audit log.")
        return SQLiteDatabase(Path(path))
    if scheme in {"postgresql", "postgres"}:
        return PostgresDatabase(secret_url)
    raise ConfigError(f"Unsupported database URL scheme {scheme!r}; use sqlite or postgresql.")


def table_columns(session: Session, table: str) -> set[str]:
    """The column names of a table in the session's database; empty if there is no table."""
    if not TABLE_NAME.fullmatch(table):
        raise ValueError(f"{table!r} is not a plain table name.")
    if session.dialect is Dialect.SQLITE:
        # PRAGMA arguments cannot be bound, so the name is checked above.
        return {row[1] for row in session.execute(f"PRAGMA table_info({table})")}
    return {
        row[0]
        for row in session.execute(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = to_regclass(?) AND attnum > 0 AND NOT attisdropped",
            (table,),
        )
    }


def require_current_table(session: Session, table: str, column: str) -> None:
    """Raise ConfigError if `table` exists but predates `column`, added in 0.1.0a2.

    The library never alters an existing table, so a table made by 0.1.0a1 is
    refused rather than migrated in place.
    """
    columns = table_columns(session, table)
    if columns and column not in columns:
        raise ConfigError(
            f"Table {table} was created by agent-core 0.1.0a1 and has no {column} column; "
            "this version does not change existing tables. Keep that database, and check "
            "its records with 0.1.0a1, then point this version at a new database."
        )


def driver_errors() -> tuple[type[Exception], ...]:
    """The database drivers' own error classes, for callers that report rather than crash."""
    errors: tuple[type[Exception], ...] = (sqlite3.Error,)
    try:
        import psycopg
    except ImportError:
        return errors
    return (*errors, psycopg.Error)


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
    "driver_errors",
    "install_postgres_schema",
    "open_database",
]
