"""SQL storage shared by the audit log and the approval queue.

Both backends are async. Postgres runs on psycopg 3's async API and a connection
pool: the library opens its own pool lazily from a URL, or uses a pool the host
passes (`PostgresDatabase.from_pool`), so no operation opens a connection. A host
can also run the library's writes inside a transaction it controls, by passing its
own connection to a call (`connection=`); see `PostgresDatabase.run_on`.

SQLite (standard library) keeps one connection per Database and runs each call on
a worker thread, one transaction at a time.

When the audit log and the approval queue share a Database, the queue writes its
audit events in the same transaction as the change they describe.
"""

import asyncio
import os
import sqlite3
import threading
import weakref
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import urlsplit

from pydantic import SecretStr

from aox_agent_core import _postgres_schema as layout
from aox_agent_core._postgres_schema import GRANTS_SQL, Grant, InstallReport
from aox_agent_core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ResultT = TypeVar("ResultT")

SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
POSTGRES_DEFAULT_PORT = 5432
# Row-value comparisons, which approval listing uses, arrived in SQLite 3.15.
SQLITE_MINIMUM_VERSION = (3, 15, 0)
POSTGRES_MINIMUM_VERSION_NUM = layout.POSTGRES_MINIMUM_VERSION_NUM
DEFAULT_MAX_CONNECTIONS = 10


class Dialect(StrEnum):
    """The SQL dialects the library supports."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


class _Executor(Protocol):
    async def run(
        self, sql: str, parameters: Sequence[Any]
    ) -> tuple[list[tuple[Any, ...]], int]: ...


class Session:
    """One open transaction. SQL is written with '?' placeholders for both dialects."""

    def __init__(self, executor: _Executor, dialect: Dialect) -> None:
        self._executor = executor
        self.dialect = dialect

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run one statement and return its rows (empty for statements without rows)."""
        rows, _ = await self._executor.run(sql, parameters)
        return rows

    async def execute_count(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """Run one statement and return how many rows it changed."""
        _, count = await self._executor.run(sql, parameters)
        return count


class _SQLiteExecutor:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._cursor = connection.cursor()

    async def run(self, sql: str, parameters: Sequence[Any]) -> tuple[list[tuple[Any, ...]], int]:
        return await asyncio.to_thread(self._run, sql, tuple(parameters))

    def _run(self, sql: str, parameters: tuple[Any, ...]) -> tuple[list[tuple[Any, ...]], int]:
        self._cursor.execute(sql, parameters)
        rows = (
            [tuple(row) for row in self._cursor.fetchall()]
            if self._cursor.description is not None
            else []
        )
        return rows, int(self._cursor.rowcount)


class _PostgresExecutor:
    """Runs statements with prepare=False: server-side prepared statements belong to
    one backend connection, which a transaction-mode pooler hands to other clients."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    async def run(self, sql: str, parameters: Sequence[Any]) -> tuple[list[tuple[Any, ...]], int]:
        if parameters:
            # psycopg uses %s placeholders, so a literal % must be doubled.
            await self._cursor.execute(
                sql.replace("%", "%%").replace("?", "%s"), tuple(parameters), prepare=False
            )
        else:
            # Without parameters psycopg sends the text as it is.
            await self._cursor.execute(sql, prepare=False)
        rows = (
            [tuple(row) for row in await self._cursor.fetchall()]
            if self._cursor.description is not None
            else []
        )
        return rows, int(self._cursor.rowcount)


class Database(ABC):
    """A database the library's tables live in."""

    dialect: Dialect

    async def run(
        self, work: Callable[[Session], Awaitable[ResultT]], *, write: bool = False
    ) -> ResultT:
        """Run `work` in one transaction and return its result.

        Commits if `work` returns, rolls back if it raises. On SQLite a write
        transaction takes the database's write lock up front (BEGIN IMMEDIATE), so
        concurrent writers queue instead of failing later. Postgres has no
        equivalent here; writers that must be serialized take their own lock (the
        audit log uses an advisory lock).
        """
        async with self._transaction(write=write) as session:
            return await work(session)

    async def run_on(
        self, connection: Any, work: Callable[[Session], Awaitable[ResultT]]
    ) -> ResultT:
        """Run `work` inside the transaction the host already has open on `connection`."""
        raise ConfigError(
            "Running inside a host transaction needs Postgres and a psycopg AsyncConnection."
        )

    async def aclose(self) -> None:  # noqa: B027 - closing is optional for a backend
        """Release what this object opened (a connection pool, a SQLite connection)."""

    async def __aenter__(self) -> "Database":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def same_database(self, other: "Database") -> bool:
        """True when both point at the same database, even as separate objects."""
        return self._identity() == other._identity()

    @abstractmethod
    def _identity(self) -> tuple[str, ...]: ...

    @abstractmethod
    def _transaction(self, *, write: bool) -> AbstractAsyncContextManager[Session]: ...


def run_blocking(make: Callable[[], Awaitable[ResultT]]) -> ResultT:
    """Run a coroutine to completion from synchronous code, on its own event loop.

    From inside a running loop it uses a short-lived thread, so it never raises
    "asyncio.run() cannot be called from a running event loop". Whatever the
    coroutine opens (a Database, a pool) must be closed inside it.
    """

    async def call() -> ResultT:
        return await make()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(call())
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, call()).result()


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
        self._connection: sqlite3.Connection | None = None
        self._connect_lock = threading.Lock()
        self._finalizer: Any = None
        self._locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    def _identity(self) -> tuple[str, ...]:
        return (self.dialect.value, str(self.path.resolve()))

    def _loop_lock(self) -> asyncio.Lock:
        # One transaction at a time on the one connection; a lock per event loop,
        # since a lock may not be shared between loops.
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def _open(self, *, write: bool) -> sqlite3.Connection:
        """The connection for one transaction, on a worker thread."""
        with self._connect_lock:
            exists = self.path.exists()
            if not write and not exists:
                # A file that does not exist reads as an empty, read-only database:
                # reading never creates it, so a mistyped path is not silently turned
                # into a new log, and a write sent as a read fails instead of vanishing.
                return sqlite3.connect(
                    "file::memory:?mode=ro", uri=True, isolation_level=None, check_same_thread=False
                )
            if self._connection is not None and not exists:
                # The file was removed under us; a connection to its old inode is not it.
                self._close_connection()
            if self._connection is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._connection = sqlite3.connect(
                    self.path,
                    timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                    isolation_level=None,
                    check_same_thread=False,
                )
                self._finalizer = weakref.finalize(self, self._connection.close)
            return self._connection

    def _close_connection(self) -> None:
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None
        self._connection = None

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_connection)

    @asynccontextmanager
    async def _transaction(self, *, write: bool) -> "AsyncIterator[Session]":
        async with self._loop_lock():
            connection = await asyncio.to_thread(self._open, write=write)
            is_temporary = connection is not self._connection
            try:
                await asyncio.to_thread(connection.execute, "BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield Session(_SQLiteExecutor(connection), self.dialect)
                except BaseException:
                    await asyncio.to_thread(connection.execute, "ROLLBACK")
                    raise
                await asyncio.to_thread(connection.execute, "COMMIT")
            finally:
                if is_temporary:
                    await asyncio.to_thread(connection.close)


class ConnectionSource(Protocol):
    """What a host's pool must offer: `connection()`, an async context manager that
    yields a psycopg AsyncConnection and gives it back afterwards.
    psycopg_pool.AsyncConnectionPool does."""

    def connection(self) -> AbstractAsyncContextManager[Any]: ...


class PostgresDatabase(Database):
    """A Postgres database, reached with the `postgres` extra (psycopg 3 and its pool).

    From a URL it owns a pool that opens on first use, on the running event loop;
    close it with `await database.aclose()` (or `async with`). From a host's pool
    (`from_pool`) it only borrows connections and never closes the pool.
    """

    dialect = Dialect.POSTGRES

    def __init__(
        self,
        url: SecretStr | None = None,
        *,
        pool: ConnectionSource | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        if (url is None) == (pool is None):
            raise ConfigError("Give PostgresDatabase a URL or a pool, not both and not neither.")
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        self._url = url
        self._host_pool = pool
        self._max_connections = max_connections
        self._psycopg = _import_psycopg()
        self._owned: Any = None
        self._owned_loop: asyncio.AbstractEventLoop | None = None
        self._opened = False
        self._open_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    @classmethod
    def from_pool(cls, pool: ConnectionSource) -> "PostgresDatabase":
        """Use a pool the host owns, such as a psycopg_pool.AsyncConnectionPool."""
        return cls(pool=pool)

    def _identity(self) -> tuple[str, ...]:
        if self._url is None:
            return (self.dialect.value, f"pool={id(self._host_pool)}")
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

    async def _source(self) -> ConnectionSource:
        if self._host_pool is not None:
            return self._host_pool
        loop = asyncio.get_running_loop()
        if self._opened and self._owned_loop is loop:
            return self._owned  # type: ignore[no-any-return]
        # The first use opens the pool; concurrent first uses wait for it.
        lock = self._open_locks.get(loop)
        if lock is None:
            lock = self._open_locks[loop] = asyncio.Lock()
        async with lock:
            if self._owned is not None and self._owned_loop is not loop:
                if self._owned_loop is not None and not self._owned_loop.is_closed():
                    raise ConfigError(
                        "This Database's pool was opened on another running event loop; "
                        "use one Database per event loop."
                    )
                # Its loop is gone (a script that ran asyncio.run twice); start afresh.
                self._owned = None
                self._opened = False
            if not self._opened:
                await self._open_pool(loop)
        return self._owned  # type: ignore[no-any-return]

    async def _open_pool(self, loop: asyncio.AbstractEventLoop) -> None:
        import psycopg_pool

        assert self._url is not None  # noqa: S101 - set whenever there is no host pool
        # A pool retries a refused connection until its timeout and then reports only
        # that it timed out. Connect once directly first, so a wrong host, port or
        # password fails at once with the driver's own error.
        probe = await self._psycopg.AsyncConnection.connect(self._url.get_secret_value())
        await probe.close()
        pool = psycopg_pool.AsyncConnectionPool(
            self._url.get_secret_value(),
            min_size=1,
            max_size=self._max_connections,
            open=False,
            check=psycopg_pool.AsyncConnectionPool.check_connection,
            kwargs={"autocommit": False, "prepare_threshold": None},
        )
        await pool.open()
        self._owned, self._owned_loop, self._opened = pool, loop, True

    async def aclose(self) -> None:
        """Close the pool this object opened. A host's pool is never closed here."""
        owned, loop = self._owned, self._owned_loop
        self._owned, self._owned_loop, self._opened = None, None, False
        if owned is None or loop is None or loop.is_closed():
            # Nothing was opened, or its event loop is gone and took its tasks with it.
            return
        if loop is not asyncio.get_running_loop():
            raise ConfigError("Close a Database on the event loop that opened its pool.")
        await owned.close()

    @asynccontextmanager
    async def _transaction(self, *, write: bool) -> "AsyncIterator[Session]":
        source = await self._source()
        async with (
            source.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            # Everything below is per transaction, never per session: a pooler in
            # transaction mode hands this backend connection to other clients
            # between transactions, and their session settings reach us too.
            #
            # Appends read the head and insert after it under an advisory lock;
            # that is only safe in READ COMMITTED, whatever the server defaults to.
            #
            # Nothing the library runs resolves a name through search_path: every
            # table is schema-qualified. Pinning it means an object another role
            # created in a schema this role searches can never stand in for a
            # catalog function or operator in the library's own statements.
            await cursor.execute(
                "SET TRANSACTION ISOLATION LEVEL READ COMMITTED; "
                "SET LOCAL search_path = pg_catalog, pg_temp",
                prepare=False,
            )
            yield Session(_PostgresExecutor(cursor), self.dialect)

    async def run_on(
        self, connection: Any, work: Callable[[Session], Awaitable[ResultT]]
    ) -> ResultT:
        """Run `work` on `connection`, inside the transaction the host has open there.

        Nothing here commits, rolls back or closes the host's transaction: the
        work runs in a savepoint, so a failure undoes only the library's part, and
        the host's commit or rollback decides everything else, including the audit
        records. The connection must already be in a transaction at READ COMMITTED,
        or ConfigError is raised before anything is written: the audit log reads the
        chain head after taking its lock, which needs a fresh snapshot per statement.

        The advisory lock an audit write takes lasts until the host's transaction
        ends, so write audit events late in it. Records returned from inside it
        are provisional until it commits.
        """
        if not isinstance(connection, self._psycopg.AsyncConnection):
            raise ConfigError("connection must be a psycopg AsyncConnection.")
        status = connection.info.transaction_status
        if status != self._psycopg.pq.TransactionStatus.INTRANS:
            raise ConfigError(
                "The connection is not in a healthy open transaction (it is "
                f"{status.name}). Open one with `async with connection.transaction():` so "
                "the library's writes commit or roll back with yours."
            )
        async with connection.transaction(), connection.cursor() as cursor:
            executor = _PostgresExecutor(cursor)
            isolation, path = (
                await executor.run(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('search_path')",
                    (),
                )
            )[0][0]
            if isolation != "read committed":
                raise ConfigError(
                    f"The host transaction is {isolation.upper()}; agent-core's audit writes "
                    "need READ COMMITTED."
                )
            # Pinned for this savepoint only: restored below on success, and undone by
            # the savepoint's rollback on failure, so it never reaches the host's own
            # statements.
            await executor.run("SELECT set_config('search_path', 'pg_catalog, pg_temp', true)", ())
            result = await work(Session(executor, self.dialect))
            await executor.run("SELECT set_config('search_path', ?, true)", (path,))
            return result


def open_database(
    url: str | SecretStr, *, max_connections: int = DEFAULT_MAX_CONNECTIONS
) -> Database:
    """Open a database from a URL: sqlite:///relative/path, sqlite:////absolute/path,
    or postgresql://user@host:port/name.

    A Postgres URL may carry a password, so it is kept as a SecretStr. A Postgres
    database opens its pool on first use and should be closed with `aclose()`.
    """
    secret_url = url if isinstance(url, SecretStr) else SecretStr(url)
    text = secret_url.get_secret_value()
    scheme = urlsplit(text).scheme
    if scheme == "sqlite":
        path = text.removeprefix("sqlite:///")
        if not path or path == text:
            raise ConfigError("A SQLite URL looks like sqlite:///audit.sqlite3.")
        if path == ":memory:" or path.startswith("file::memory"):
            # An in-memory database is private to one connection and gone with it.
            raise ConfigError("An in-memory SQLite database cannot hold the audit log.")
        return SQLiteDatabase(Path(path))
    if scheme in {"postgresql", "postgres"}:
        return PostgresDatabase(secret_url, max_connections=max_connections)
    raise ConfigError(f"Unsupported database URL scheme {scheme!r}; use sqlite or postgresql.")


@dataclass(frozen=True)
class TableName:
    """One of the library's tables: bare on SQLite, schema-qualified on Postgres."""

    name: str
    schema: str | None = None

    @classmethod
    def on(cls, database: "Database", name: str, schema: str | None) -> "TableName":
        """The table `name` in `schema` (public by default) on Postgres; on SQLite,
        where there are no schemas, ConfigError if a schema is given."""
        if database.dialect is Dialect.SQLITE:
            if schema is not None:
                raise ConfigError("SQLite has no schemas; leave schema unset.")
            return cls(name)
        chosen = schema if schema is not None else layout.DEFAULT_SCHEMA
        layout.identifier(chosen, what="schema")
        return cls(name, chosen)

    @property
    def sql(self) -> str:
        """The name as it goes into SQL, and into to_regclass()."""
        return f'"{self.schema}".{self.name}' if self.schema is not None else self.name

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema is not None else self.name


async def table_columns(session: Session, table: str, *, schema: str | None = None) -> set[str]:
    """The column names of a table, in `schema` on Postgres; empty if there is no table."""
    names = (table, schema) if schema is not None else (table,)
    if not all(layout.IDENTIFIER.fullmatch(name) for name in names):
        raise ValueError(f"{table!r} in {schema!r} is not a plain table name.")
    if session.dialect is Dialect.SQLITE:
        # PRAGMA arguments cannot be bound, so the name is checked above.
        return {row[1] for row in await session.execute(f"PRAGMA table_info({table})")}
    # search_path is pinned to pg_catalog, so a bare name would find nothing.
    qualified = f'"{schema if schema is not None else layout.DEFAULT_SCHEMA}".{table}'
    return {
        row[0]
        for row in await session.execute(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = to_regclass(?) AND attnum > 0 AND NOT attisdropped",
            (qualified,),
        )
    }


async def require_current_table(
    session: Session,
    table: str,
    column: str,
    *,
    columns: set[str] | None = None,
    schema: str | None = None,
) -> None:
    """Raise ConfigError if `table` exists but predates `column`, added in 0.1.0a2.

    A table made by 0.1.0a1 is refused rather than migrated in place.
    """
    columns = columns if columns is not None else await table_columns(session, table, schema=schema)
    if columns and column not in columns:
        raise ConfigError(
            f"Table {table} was created by agent-core 0.1.0a1 and has no {column} column; "
            "this version does not change existing tables. Keep that database, and check "
            "its records with 0.1.0a1, then point this version at a new database."
        )


async def bring_table_up_to_date(
    session: Session,
    table: str,
    additions: Mapping[str, str],
    *,
    schema: str | None = None,
) -> None:
    """Refuse a 0.1.0a1 table; add the columns later releases added, or ask for the installer.

    `additions` maps each column added in 0.1.0a3 or 0.1.0a4 to its SQLite type. On SQLite,
    where the library owns its tables, missing columns are added in place. On
    Postgres the application role cannot alter tables, so a missing column means
    the schema predates this release and the installer must upgrade it.
    """
    columns = await table_columns(session, table, schema=schema)
    if not columns:
        return
    await require_current_table(session, table, "run_context", columns=columns)
    missing = [column for column in additions if column not in columns]
    if not missing:
        return
    if session.dialect is Dialect.POSTGRES:
        raise ConfigError(
            f"Table {table} was created by an earlier agent-core and has no {', '.join(missing)} "
            "column. As the owner role, run install_postgres_schema from 0.1.0a4 with the "
            "requester and approver roles: it upgrades the schema in place and keeps every row."
        )
    for column in missing:
        # Column names and types are the library's own constants.
        await session.execute(f"ALTER TABLE {table} ADD COLUMN {column} {additions[column]}")


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


def install_postgres_schema(
    owner_url: str | SecretStr,
    *,
    requester_role: str,
    approver_role: str,
    schema: str = "public",
    close_unaudited_approvals: bool = False,
) -> InstallReport:
    """Create or upgrade the audit and approval tables in `schema`, as the owner role.

    Run by an operator, never by the application, after creating the two roles.
    The requester role (the agent side) may submit, consume and cancel; the
    approver role (the decision side) may approve or reject; a guard trigger
    enforces that in the database. See aox_agent_core._postgres_schema for the
    layout and the transitions.

    It is idempotent: a second run with the same roles changes nothing. It
    upgrades a 0.1.0a2 schema in place, keeping every row. It grants a role its
    layout only on a table where that role holds no privilege yet, and never
    revokes anything, so it never weakens a grant an operator tightened. The
    returned report lists grants outside the layout, such as an a2 app role's,
    for the operator to revoke, and the approved, unused requests that no
    approval.resolved audit event approves (what plain SQL could have approved
    under 0.1.0a2). With close_unaudited_approvals=True it also cancels those,
    in the same transaction, and lists them as closed. Raises ConfigError for
    tables from 0.1.0a1, for role names other than those an earlier run
    recorded, for roles that overlap, or while the requester role can create
    objects in the schema or in public. Needs Postgres 16 or later.
    """
    layout.identifier(requester_role, what="role")
    layout.identifier(approver_role, what="role")
    layout.identifier(schema, what="schema")
    if requester_role == approver_role:
        raise ConfigError("The requester and approver roles must be different roles.")
    if urlsplit(
        owner_url.get_secret_value() if isinstance(owner_url, SecretStr) else owner_url
    ).scheme not in {"postgresql", "postgres"}:
        raise ConfigError("install_postgres_schema needs a postgresql:// URL.")

    async def install(session: Session) -> InstallReport:
        await layout.require_postgres_version(session)
        await session.execute("SELECT pg_advisory_xact_lock(?)", (layout.INSTALL_LOCK_KEY,))
        await _refuse_overlapping_roles(session, requester_role, approver_role)
        await layout.refuse_requester_create(session, requester_role, schema)
        await _refuse_tables_from_0_1_0a1(session, schema)
        if not await session.execute("SELECT 1 FROM pg_namespace WHERE nspname = ?", (schema,)):
            await session.execute(f"CREATE SCHEMA {layout.identifier(schema, what='schema')}")
        for statement in (*layout.audit_ddl(schema), *layout.approvals_tables_ddl(schema)):
            await session.execute(statement)
        # Checked before the guard is written: a run with other role names must not
        # get as far as rewriting it with them.
        await _record_roles(session, schema, requester_role, approver_role)
        for statement in layout.approvals_guard_ddl(schema, requester_role, approver_role):
            await session.execute(statement)
        for role, role_layout in (
            (requester_role, layout.REQUESTER_LAYOUT),
            (approver_role, layout.APPROVER_LAYOUT),
            ("PUBLIC", layout.PUBLIC_LAYOUT),
        ):
            # Decided per table before granting anything, so a role's first grant on
            # a table does not stop the rest of its layout there.
            fresh_tables = {
                table
                for table in role_layout
                if not await _holds_any_grant(session, schema, table, role)
            }
            for table, statement in layout.grant_statements(schema, role, role_layout):
                if table in fresh_tables:
                    await session.execute(statement)
            # Both roles must reach the schema; in public, PUBLIC usually grants it.
            if (
                role != "PUBLIC"
                and not (
                    await session.execute(
                        "SELECT has_schema_privilege(?, ?, 'USAGE')", (role, schema)
                    )
                )[0][0]
            ):
                await session.execute(
                    f"GRANT USAGE ON SCHEMA {layout.identifier(schema, what='schema')} "
                    f"TO {layout.identifier(role, what='role')}"
                )
        unaudited = await _unaudited_approvals(session, schema, approver_role)
        if close_unaudited_approvals and unaudited:
            await _refuse_closing_without_local_audit(session, schema, approver_role)
            await _cancel_as_owner(session, schema, unaudited)
        return InstallReport(
            schema=schema,
            requester_role=requester_role,
            approver_role=approver_role,
            outside_layout=tuple(
                await outside_layout(session, schema, requester_role, approver_role)
            ),
            unaudited_approvals=tuple(unaudited),
            closed_approvals=tuple(unaudited) if close_unaudited_approvals else (),
        )

    async def run() -> InstallReport:
        async with PostgresDatabase(
            owner_url if isinstance(owner_url, SecretStr) else SecretStr(owner_url),
            max_connections=1,
        ) as database:
            return await database.run(install, write=True)

    return run_blocking(run)


# Canonical JSON (sorted keys, no spaces) puts the decision exactly so in a
# resolved event's payload; matching text needs no cast of rows a2 may have left.
# Only an event the approver side wrote counts, or one from before db_role existed:
# the requester role may append audit events too.
_FROM_THE_APPROVER_SIDE = """(e.db_role IS NULL OR EXISTS (
    SELECT 1 FROM pg_roles r WHERE r.rolname = e.db_role AND pg_has_role(r.oid, ?, 'MEMBER')
))"""
_UNAUDITED_APPROVALS_SQL = f"""
SELECT a.id FROM {{approvals}} a
WHERE a.status = 'approved'
  AND NOT EXISTS (
    SELECT 1 FROM {{audit}} e
    WHERE e.action = 'approval.resolved' AND e.subject_id = a.id
      AND e.payload LIKE '%"decision":"approve"%'
      AND {_FROM_THE_APPROVER_SIDE}
  )
ORDER BY a.id
"""


async def _unaudited_approvals(session: Session, schema: str, approver_role: str) -> list[str]:
    """Approved, unused requests that no approval.resolved event from the approver side
    (or from before 0.1.0a3) approves."""
    sql = _UNAUDITED_APPROVALS_SQL.format(**_qualified_tables(schema))
    return [row[0] for row in await session.execute(sql, (approver_role,))]


def _qualified_tables(schema: str) -> dict[str, str]:
    quoted = layout.identifier(schema, what="schema")
    return {
        "approvals": f"{quoted}.{layout.APPROVALS_TABLE}",
        "audit": f"{quoted}.{layout.AUDIT_TABLE}",
    }


async def _refuse_closing_without_local_audit(
    session: Session, schema: str, approver_role: str
) -> None:
    """Closing relies on resolved events in this schema's audit table. With none there
    from the approver side, the audit log lives elsewhere, and every live approval
    would look unaudited. Events the requester appended do not count."""
    audit = _qualified_tables(schema)["audit"]
    if not await session.execute(
        f"SELECT 1 FROM {audit} e WHERE e.action = 'approval.resolved' "
        f"AND {_FROM_THE_APPROVER_SIDE} LIMIT 1",
        (approver_role,),
    ):
        raise ConfigError(
            f"close_unaudited_approvals needs the approval.resolved events in {schema}'s "
            "audit table, and it holds none: the audit log may live elsewhere. Nothing was "
            "changed. Check the listed requests yourself instead."
        )


async def _cancel_as_owner(session: Session, schema: str, request_ids: list[str]) -> None:
    """Cancel requests the guard would refuse to touch, inside the install transaction.

    The owner switches the guard off for this one statement and back on before
    the transaction commits, so no other session ever sees it off.
    """
    table = f"{layout.identifier(schema, what='schema')}.{layout.APPROVALS_TABLE}"
    await session.execute(f"ALTER TABLE {table} DISABLE TRIGGER {layout.APPROVALS_GUARD_TRIGGER}")
    for request_id in request_ids:
        # A cancelled request carries no decision, so the reason keeps who approved it.
        await session.execute(
            f"UPDATE {table} SET status = 'cancelled', decision = NULL, resolved_by = NULL, "
            "resolved_at = NULL, consumed_at = NULL, "
            "reason = left(format('Cancelled by install_postgres_schema: approved by %s at %s, "
            "with no approval.resolved audit event from the approver side.', "
            "coalesce(resolved_by, 'nobody recorded'), coalesce(resolved_at, 'no recorded "
            "time')), 500), "
            "closed_at = to_char(statement_timestamp() AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') WHERE id = ? AND status = 'approved'",
            (request_id,),
        )
    await session.execute(f"ALTER TABLE {table} ENABLE TRIGGER {layout.APPROVALS_GUARD_TRIGGER}")


async def outside_layout(
    session: Session, schema: str, requester_role: str, approver_role: str
) -> list[Grant]:
    """Grants on the library's tables beyond the owner's and the two roles' layouts."""
    layouts = {
        requester_role: layout.REQUESTER_LAYOUT,
        approver_role: layout.APPROVER_LAYOUT,
        "PUBLIC": layout.PUBLIC_LAYOUT,
    }
    found: list[Grant] = []
    for table in (layout.AUDIT_TABLE, layout.APPROVALS_TABLE, layout.ROLES_TABLE):
        for grant in await _grants_on(session, schema, table):
            role_layout = layouts.get(grant.role)
            if role_layout is None or not layout.within_layout(grant, role_layout):
                found.append(grant)
    return found


async def _grants_on(session: Session, schema: str, table: str) -> list[Grant]:
    """Direct grants on a table and its columns, the owner's own left out."""
    qualified = f'"{schema}".{table}'
    owner_rows = await session.execute(
        "SELECT relowner FROM pg_class WHERE oid = to_regclass(?)", (qualified,)
    )
    if not owner_rows:
        return []
    owner = owner_rows[0][0]
    grants = []
    for grantee, privilege, column in await session.execute(GRANTS_SQL, (qualified, qualified)):
        if grantee == owner:
            continue
        role = (
            "PUBLIC"
            if grantee == 0
            else (await session.execute("SELECT rolname FROM pg_roles WHERE oid = ?", (grantee,)))[
                0
            ][0]
        )
        grants.append(Grant(table=table, role=role, privilege=privilege, column=column))
    return sorted(grants, key=lambda grant: (grant.role, grant.privilege, grant.column or ""))


async def _holds_any_grant(session: Session, schema: str, table: str, role: str) -> bool:
    return any(grant.role == role for grant in await _grants_on(session, schema, table))


async def _refuse_overlapping_roles(
    session: Session, requester_role: str, approver_role: str
) -> None:
    for role in (requester_role, approver_role):
        if not await session.execute("SELECT 1 FROM pg_roles WHERE rolname = ?", (role,)):
            raise ConfigError(f"Role {role} does not exist; create it before installing.")
    rows = await session.execute(
        "SELECT pg_has_role(?, ?, 'MEMBER') OR pg_has_role(?, ?, 'MEMBER'), "
        "current_user IN (?, ?), "
        "(SELECT rolsuper FROM pg_roles WHERE rolname = ?) "
        "OR (SELECT rolsuper FROM pg_roles WHERE rolname = ?)",
        (
            requester_role,
            approver_role,
            approver_role,
            requester_role,
            requester_role,
            approver_role,
            requester_role,
            approver_role,
        ),
    )
    overlapping, is_owner, is_superuser = rows[0]
    if overlapping:
        raise ConfigError(
            f"{requester_role} and {approver_role} overlap: one is a member of the other, so "
            "the requester could act as the approver. Use two unrelated roles."
        )
    if is_owner:
        raise ConfigError("Install as the owner role, not as the requester or approver role.")
    if is_superuser:
        raise ConfigError("The requester and approver roles must not be superusers.")


async def _refuse_tables_from_0_1_0a1(session: Session, schema: str) -> None:
    for table in (layout.AUDIT_TABLE, layout.APPROVALS_TABLE):
        columns = await table_columns(session, table, schema=schema)
        if columns and "run_context" not in columns:
            raise ConfigError(
                f"Table {schema}.{table} was created by agent-core 0.1.0a1, which this "
                "installer cannot upgrade. Keep that database and install into a new one."
            )


async def _record_roles(
    session: Session, schema: str, requester_role: str, approver_role: str
) -> None:
    """Store the two role names on the first run; refuse different names later."""
    roles = f'"{schema}".{layout.ROLES_TABLE}'
    await session.execute(
        f"INSERT INTO {roles} (requester_role, approver_role) VALUES (?, ?) "
        "ON CONFLICT (singleton) DO NOTHING",
        (requester_role, approver_role),
    )
    recorded = (await session.execute(f"SELECT requester_role, approver_role FROM {roles}"))[0]
    if tuple(recorded) != (requester_role, approver_role):
        raise ConfigError(
            f"Schema {schema} was installed with requester role {recorded[0]} and approver "
            f"role {recorded[1]}; the installer will not add others. Pass the same roles."
        )


__all__ = [
    "ConnectionSource",
    "Database",
    "Dialect",
    "Grant",
    "InstallReport",
    "PostgresDatabase",
    "SQLiteDatabase",
    "Session",
    "driver_errors",
    "install_postgres_schema",
    "open_database",
]
