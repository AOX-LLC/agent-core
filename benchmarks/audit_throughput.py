"""Wall time for concurrent audit appends against Postgres.

Runs on agent-core 0.1.0a3 and later. Features added after a3 (a batch append, an
async close on the database) are used only when present, so the same file gives the
"before" and "after" numbers. Data is synthetic. Needs a superuser URL in
AGENT_CORE_TEST_POSTGRES_ADMIN_URL.
"""

import argparse
import asyncio
import inspect
import json
import os
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg

from aox_agent_core import __version__
from aox_agent_core.audit import AuditEvent
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.storage import install_postgres_schema, open_database

ADMIN_URL_ENV = "AGENT_CORE_TEST_POSTGRES_ADMIN_URL"
OWNER_ROLE = "agent_core_owner"
REQUESTER_ROLE = "agent_core_requester"
APPROVER_ROLE = "agent_core_approver"
TMPFS_WARNING = "test Postgres runs on tmpfs; commit (fsync) cost is understated"

# Role names are constants defined above; nothing here comes from input.
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
END $$"""  # noqa: S608 (constant role names only)


def _with(url: str, *, user: str | None = None, database: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        netloc = f"{user}@{netloc.rpartition('@')[2]}"
    path = f"/{database}" if database is not None else parts.path
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


def _events(count: int, tag: str) -> list[AuditEvent]:
    return [
        AuditEvent(action="bench.append", actor_id=f"bench-{tag}", payload={"n": n})
        for n in range(count)
    ]


async def _close(database: Any) -> None:
    aclose = getattr(database, "aclose", None)
    if aclose is not None and inspect.iscoroutinefunction(aclose):
        await aclose()


async def _timed(work: Callable[[], Awaitable[object]]) -> float:
    started = time.perf_counter()
    await work()
    return time.perf_counter() - started


def _summary(samples: list[float], count: int) -> dict[str, float]:
    return {
        "min_s": min(samples),
        "median_s": statistics.median(samples),
        "max_s": max(samples),
        "median_ms_per_append": statistics.median(samples) / count * 1000,
    }


async def _run(admin_url: str, count: int, repeat: int) -> dict[str, Any]:
    name = f"agent_core_bench_{uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(_CREATE_ROLES)
        admin.execute(f'CREATE DATABASE "{name}" OWNER {OWNER_ROLE}')
        row = admin.execute("SHOW server_version").fetchone()
    server_version = str(row[0]) if row else "unknown"
    try:
        install_postgres_schema(
            _with(admin_url, user=OWNER_ROLE, database=name),
            requester_role=REQUESTER_ROLE,
            approver_role=APPROVER_ROLE,
        )
        return await _measure(
            _with(admin_url, user=REQUESTER_ROLE, database=name), count, repeat, server_version
        )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


async def _measure(url: str, count: int, repeat: int, server_version: str) -> dict[str, Any]:
    database = open_database(url)
    log = SQLAuditLog(database)
    appended = 0
    gather_samples: list[float] = []
    batch_samples: list[float] = []
    batch_logs: list[Any] = []
    try:
        # Warm-up: first write checks the role's protections and opens a connection.
        await log.append(_events(1, "warmup")[0])
        appended += 1
        for run in range(repeat):
            events = _events(count, f"gather-{run}")
            gather_samples.append(
                await _timed(lambda e=events: asyncio.gather(*(log.append(x) for x in e)))  # type: ignore[misc]
            )
            appended += count
        if hasattr(log, "append_many"):
            for run in range(repeat):
                batch_database = open_database(url)
                batch_log = SQLAuditLog(batch_database)
                batch_logs.append(batch_database)
                await batch_log.append(_events(1, "warmup")[0])
                appended += 1
                events = _events(count, f"batch-{run}")
                batch_samples.append(await _timed(lambda e=events, b=batch_log: b.append_many(e)))  # type: ignore[misc]
                appended += count
        head = await log.verify()
        if head.seq != appended:
            raise RuntimeError(f"chain head seq {head.seq} != {appended} appended records")
    finally:
        for batch_database in batch_logs:
            await _close(batch_database)
        await _close(database)
    result: dict[str, Any] = {
        "aox_agent_core_version": __version__,
        "postgres_version": server_version,
        "count": count,
        "repeat": repeat,
        "warning": TMPFS_WARNING,
        "gather_append": {**_summary(gather_samples, count), "samples_s": gather_samples},
        "verify": {"head_seq": head.seq, "appended": appended, "gapless": True},
    }
    if batch_samples:
        result["append_many"] = {**_summary(batch_samples, count), "samples_s": batch_samples}
    return result


def _print(result: dict[str, Any]) -> None:
    print(f"aox_agent_core {result['aox_agent_core_version']}")
    print(f"postgres {result['postgres_version']}")
    print(f"WARNING: {result['warning']}")
    print(f"appends per run: {result['count']}, runs: {result['repeat']}")
    print()
    header = f"{'method':<28}{'min s':>9}{'median s':>10}{'max s':>9}{'ms/append':>11}"
    print(header)
    print("-" * len(header))
    for key, label in (
        ("gather_append", "gather(append) x N"),
        ("append_many", "append_many(N)"),
    ):
        if key in result:
            row = result[key]
            print(
                f"{label:<28}{row['min_s']:>9.3f}{row['median_s']:>10.3f}"
                f"{row['max_s']:>9.3f}{row['median_ms_per_append']:>11.2f}"
            )
    verify = result["verify"]
    print()
    print(
        f"verify: chain gapless, head seq {verify['head_seq']} == "
        f"{verify['appended']} appended records"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50, help="concurrent appends per run")
    parser.add_argument("--repeat", type=int, default=5, help="timed runs")
    parser.add_argument("--json", type=Path, default=None, metavar="PATH", help="write results")
    args = parser.parse_args()
    admin_url = os.environ.get(ADMIN_URL_ENV)
    if not admin_url:
        print(f"set {ADMIN_URL_ENV} to a Postgres superuser URL", file=sys.stderr)
        return 2
    result = asyncio.run(_run(admin_url, args.count, args.repeat))
    _print(result)
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
