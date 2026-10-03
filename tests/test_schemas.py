"""The library in a Postgres schema other than public, and refusal of schemas on SQLite."""

import asyncio
from pathlib import Path

import pytest

from aox_agent_core.approvals import ApprovalStatus, Decision, Principal, PrincipalKind
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.cli import main
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import install_postgres_schema, open_database
from databases import APPROVER_ROLE, REQUESTER_ROLE, postgres_database, split_queue

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))


async def test_the_whole_flow_runs_in_another_schema_and_leaves_public_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with postgres_database(schema="tenant_a") as database:
        assert database.owner_url is not None
        # A second install in public, the same table names one schema over.
        install_postgres_schema(
            database.owner_url, requester_role=REQUESTER_ROLE, approver_role=APPROVER_ROLE
        )
        queue = split_queue(database)

        request = await queue.submit(
            action="crm.update_contact",
            summary="s",
            payload={"contact_id": "c-1"},
            requested_by=REQUESTER,
            required_role="ops.approver",
            ttl_seconds=600,
        )
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
        consumed = await queue.consume(
            request.id,
            action="crm.update_contact",
            payload={"contact_id": "c-1"},
            principal=REQUESTER,
        )
        log = SQLAuditLog(database.database, schema="tenant_a")

        assert consumed.status is ApprovalStatus.CONSUMED
        assert (await log.verify()).seq == 3
        assert database.raw("SELECT count(*) FROM tenant_a.agent_core_approvals") == [(1,)]
        assert database.raw("SELECT count(*) FROM public.agent_core_approvals") == [(0,)]
        assert database.raw("SELECT count(*) FROM public.agent_core_audit") == [(0,)]
        assert (await SQLAuditLog(database.database).head()).seq == 0
        # The command runs its own event loop, so it runs on a thread here.
        verify = ["audit", "verify", database.url, "--schema", "tenant_a"]
        assert await asyncio.to_thread(main, verify) == 0
        assert "OK: 3 records" in capsys.readouterr().out


def test_sqlite_refuses_a_schema(tmp_path: Path) -> None:
    database = open_database(f"sqlite:///{tmp_path / 'x.sqlite3'}")

    with pytest.raises(ConfigError, match="SQLite has no schemas"):
        SQLAuditLog(database, schema="tenant_a")


@pytest.mark.parametrize("schema", ["Tenant", "a-b", 'x"; DROP TABLE y; --'])
def test_a_schema_must_be_a_plain_name(schema: str) -> None:
    database = open_database("postgresql://agent_core_requester@127.0.0.1:4202/postgres")

    with pytest.raises(ConfigError, match="not a plain Postgres schema name"):
        SQLAuditLog(database, schema=schema)
