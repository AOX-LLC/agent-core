"""The approval queue checks its database role before it does anything."""

from collections.abc import Iterator

import pytest

from aox_agent_core.approvals import ApprovalSide, Decision, Principal, PrincipalKind
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ConfigError
from aox_agent_core.storage import Database, open_database
from databases import (
    APPROVER_ROLE,
    REQUESTER_ROLE,
    ControlDatabase,
    postgres_database,
    split_queue,
)
from test_postgres_schema import load_a2_schema

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))


@pytest.fixture
def pg() -> Iterator[ControlDatabase]:
    with postgres_database() as database:
        yield database


def queue_on(database: Database) -> SQLApprovalQueue:
    return SQLApprovalQueue(database, audit_log=SQLAuditLog(database))


async def submit(queue: SQLApprovalQueue) -> object:
    return await queue.submit(
        action="crm.update_contact",
        summary="s",
        payload={},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )


async def test_each_connection_knows_its_side(control_database: ControlDatabase) -> None:
    queues = split_queue(control_database)

    sides = (await queues.requester.side(), await queues.approver.side())

    if control_database.backend == "sqlite":
        assert sides == (ApprovalSide.BOTH, ApprovalSide.BOTH)
    else:
        assert sides == (ApprovalSide.REQUESTER, ApprovalSide.APPROVER)


async def test_each_side_is_refused_the_others_operations(pg: ControlDatabase) -> None:
    queues = split_queue(pg)
    request = await queues.submit(
        action="crm.update_contact",
        summary="s",
        payload={},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )

    with pytest.raises(ConfigError, match="requester role, which cannot decide"):
        await queues.requester.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    with pytest.raises(ConfigError, match="approver role, which cannot submit"):
        await submit(queues.approver)
    with pytest.raises(ConfigError, match="approver role, which cannot consume"):
        await queues.approver.consume(
            request.id, action="crm.update_contact", payload={}, principal=REQUESTER
        )


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            [f"UPDATE agent_core_approval_roles SET approver_role = '{REQUESTER_ROLE}'"],
            "overlap",
        ),
        (
            ["ALTER TABLE agent_core_approvals DISABLE TRIGGER agent_core_approvals_guard"],
            "missing or disabled: agent_core_approvals_guard",
        ),
        (
            [f"GRANT UPDATE (decision) ON agent_core_approvals TO {REQUESTER_ROLE}"],
            "can update decision",
        ),
        (
            [f"GRANT INSERT ON agent_core_approvals TO {APPROVER_ROLE}"],
            "can insert approval requests",
        ),
        (
            [f"GRANT DELETE ON agent_core_approvals TO {REQUESTER_ROLE}"],
            "can delete from or truncate",
        ),
    ],
    ids=["same-role", "disabled-guard", "requester-decides", "approver-inserts", "delete"],
)
async def test_a_wrong_setup_is_refused_before_anything_is_written(
    pg: ControlDatabase, setup: list[str], message: str
) -> None:
    for statement in setup:
        pg.raw(statement)
    queue = queue_on(pg.database)

    with pytest.raises(ConfigError, match=message):
        await submit(queue)
    assert pg.raw("SELECT count(*) FROM agent_core_approvals") == [(0,)]
    assert pg.raw("SELECT count(*) FROM agent_core_audit") == [(0,)]


async def test_a_role_overlapping_through_membership_is_refused(pg: ControlDatabase) -> None:
    pg.login_role(f"GRANT {REQUESTER_ROLE} TO {{role}}")
    member_of_requester = pg.roles[-1]
    pg.raw(f"UPDATE agent_core_approval_roles SET approver_role = '{member_of_requester}'")

    with pytest.raises(ConfigError, match="overlap"):
        await queue_on(pg.database).side()


@pytest.mark.parametrize("membership", ["both", "neither"])
async def test_a_login_in_both_roles_or_neither_is_refused(
    pg: ControlDatabase, membership: str
) -> None:
    grants = (
        [f"GRANT {REQUESTER_ROLE}, {APPROVER_ROLE} TO {{role}}"] if membership == "both" else []
    )
    url = pg.login_role(*grants)

    with pytest.raises(ConfigError, match=f"member of {membership}"):
        await queue_on(open_database(url)).side()


async def test_a_superuser_connection_is_refused(pg: ControlDatabase) -> None:
    assert pg.superuser_url is not None

    with pytest.raises(ConfigError, match="superuser"):
        await queue_on(open_database(pg.superuser_url)).side()


async def test_an_a2_schema_asks_for_the_installer() -> None:
    with postgres_database(install=False) as database:
        load_a2_schema(database)
        database.raw(f"GRANT SELECT, INSERT, UPDATE ON agent_core_approvals TO {REQUESTER_ROLE}")

        with pytest.raises(ConfigError, match=r"0\.1\.0a2.*install_postgres_schema"):
            await queue_on(database.database).side()
