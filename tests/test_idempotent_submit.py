"""One open request per requester, action and payload: repeats return it, races yield one."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from aox_agent_core import _postgres_schema as layout
from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
)
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ApprovalConflictError, ConfigError
from aox_agent_core.storage import install_postgres_schema
from databases import APPROVER_ROLE, REQUESTER_ROLE, ControlDatabase, SplitQueue, split_queue
from test_approval_payload import ACTION, APPROVALS, raw_request
from test_approvals import APPROVER, PAYLOAD, REQUESTER

OTHER_REQUESTER = Principal(id="agent-billing", kind=PrincipalKind.AGENT)
SWEEPER = Principal(id="svc-sweeper", kind=PrincipalKind.SERVICE)


async def ask(queue: SplitQueue, **overrides: Any) -> ApprovalRequest:
    terms: dict[str, Any] = {
        "action": ACTION,
        "summary": "Update the sample contact's phone number",
        "payload": PAYLOAD,
        "requested_by": REQUESTER,
        "required_role": "ops.approver",
        "ttl_seconds": 3_600,
    }
    return await queue.submit(**{**terms, **overrides})


async def audit_actions(database: ControlDatabase) -> list[str]:
    return [r.action async for r in SQLAuditLog(database.database).iter_records()]


async def test_an_exact_repeat_returns_the_open_request_and_writes_no_event(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    first = await ask(queue, delegates={"svc-runner"})

    again = await ask(queue, delegates={"svc-runner"}, summary="A different summary")

    assert again == first  # the first call's summary stays
    assert await audit_actions(control_database) == ["approval.requested"]
    assert control_database.raw(f"SELECT count(*) FROM {APPROVALS}") == [(1,)]


@pytest.mark.parametrize(
    ("overrides", "differs"),
    [
        ({"required_role": "finance.approver"}, ("required_role",)),
        ({"ttl_seconds": 600}, ("lifetime",)),
        ({"delegates": {"svc-runner"}}, ("delegates",)),
        (
            {"required_role": "finance.approver", "ttl_seconds": 600, "delegates": {"x"}},
            ("delegates", "lifetime", "required_role"),
        ),
    ],
    ids=["role", "lifetime", "delegates", "all-three"],
)
async def test_a_repeat_with_other_terms_is_a_conflict_and_is_audited(
    control_database: ControlDatabase, overrides: dict[str, Any], differs: tuple[str, ...]
) -> None:
    queue = split_queue(control_database)
    first = await ask(queue)

    with pytest.raises(ApprovalConflictError) as raised:
        await ask(queue, **overrides)

    assert raised.value.existing == first.id
    assert raised.value.differs == differs
    assert await queue.get(first.id) == first
    assert await audit_actions(control_database) == [
        "approval.requested",
        "approval.submit_conflict",
    ]
    records = [r async for r in SQLAuditLog(control_database.database).iter_records()]
    assert records[-1].subject_id == str(first.id)
    assert records[-1].payload["differs"] == ",".join(differs)
    assert control_database.raw(f"SELECT count(*) FROM {APPROVALS}") == [(1,)]


async def test_another_requester_action_or_payload_is_another_request(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    base = await ask(queue)

    others = [
        await ask(queue, requested_by=OTHER_REQUESTER),
        await ask(queue, action="crm.delete_contact"),
        await ask(queue, payload={**PAYLOAD, "phone": "+1-555-0101"}),
    ]

    assert len({base.id, *(other.id for other in others)}) == 4


async def test_an_approved_unused_request_is_returned_and_a_finished_one_is_not(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    first = await ask(queue)
    await queue.resolve(first.id, decision=Decision.APPROVE, principal=APPROVER)

    again = await ask(queue)
    assert (again.id, again.status) == (first.id, ApprovalStatus.APPROVED)

    await queue.consume(first.id, action=ACTION, payload=PAYLOAD, principal=REQUESTER)
    fresh = await ask(queue)
    assert fresh.id != first.id
    assert fresh.status is ApprovalStatus.PENDING

    await queue.cancel(fresh.id, principal=REQUESTER)
    rejected_next = await ask(queue)
    await queue.resolve(rejected_next.id, decision=Decision.REJECT, principal=APPROVER)
    assert (await ask(queue)).id not in {first.id, fresh.id, rejected_next.id}


async def test_concurrent_identical_submits_yield_one_request(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)

    results = await asyncio.gather(*(ask(queue) for _ in range(20)))

    assert len({request.id for request in results}) == 1
    assert control_database.raw(f"SELECT count(*) FROM {APPROVALS}") == [(1,)]
    assert (await audit_actions(control_database)).count("approval.requested") == 1
    pending = await queue.list_pending(APPROVER)
    assert [request.id for request in pending] == [results[0].id]
    # One request, so one approval and one run: the second resolve and consume are refused.
    await queue.resolve(results[0].id, decision=Decision.APPROVE, principal=APPROVER)
    await queue.consume(results[0].id, action=ACTION, payload=PAYLOAD, principal=REQUESTER)
    with pytest.raises(Exception, match=r"consumed|resolved|used"):
        await queue.consume(results[0].id, action=ACTION, payload=PAYLOAD, principal=REQUESTER)


async def test_concurrent_submits_with_other_terms_yield_one_request_and_conflicts(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)

    outcomes = await asyncio.gather(
        *(ask(queue, ttl_seconds=600 + n) for n in range(8)), return_exceptions=True
    )

    made = [o for o in outcomes if isinstance(o, ApprovalRequest)]
    assert len(made) == 1
    assert all(isinstance(o, ApprovalConflictError) for o in outcomes if o is not made[0])
    assert control_database.raw(f"SELECT count(*) FROM {APPROVALS}") == [(1,)]


# A request left open past its lifetime does not block the next submit


async def test_a_pending_request_past_its_lifetime_is_expired_and_replaced(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    old = await ask(queue, ttl_seconds=1)
    await asyncio.sleep(1.3)

    fresh = await ask(queue)

    assert fresh.id != old.id
    stored = await queue.get(old.id)
    assert stored.status is ApprovalStatus.EXPIRED
    assert await audit_actions(control_database) == [
        "approval.requested",
        "approval.expired",
        "approval.requested",
    ]


async def test_an_approval_that_lapsed_unused_is_expired_and_replaced(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    old = await ask(queue, ttl_seconds=2)
    await queue.resolve(old.id, decision=Decision.APPROVE, principal=APPROVER)
    await asyncio.sleep(2.3)

    # Reads already treat it as expired, with its decision kept.
    read = await queue.get(old.id)
    assert (read.status, read.decision, read.resolved_by) == (
        ApprovalStatus.EXPIRED,
        Decision.APPROVE,
        APPROVER.id,
    )
    fresh = await ask(queue)

    assert fresh.id != old.id
    assert fresh.status is ApprovalStatus.PENDING
    stored = control_database.raw(f"SELECT status, decision FROM {APPROVALS} WHERE id = '{old.id}'")
    assert stored == [("expired", "approve")]
    records = [r async for r in SQLAuditLog(control_database.database).iter_records()]
    expired = [r for r in records if r.action == "approval.expired"]
    assert [r.payload.get("previous_status") for r in expired] == ["approved"]


async def test_the_sweep_expires_an_approval_that_lapsed_unused(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    old = await ask(queue, ttl_seconds=2)
    await queue.resolve(old.id, decision=Decision.APPROVE, principal=APPROVER)
    await asyncio.sleep(2.3)

    assert await queue.expire_due(principal=SWEEPER) == 1
    assert await queue.expire_due(principal=SWEEPER) == 0
    stored = control_database.raw(f"SELECT status, closed_at IS NOT NULL FROM {APPROVALS}")
    assert stored == [("expired", True)]


# The database's side (Postgres)


def test_the_index_exists_and_refuses_a_second_open_row_from_plain_sql(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    first = str(uuid4())
    same = {"payload_sha256": "'" + "d" * 64 + "'"}
    control_database.requester_raw(raw_request(id=f"'{first}'", **same))

    with pytest.raises(psycopg.errors.UniqueViolation):
        control_database.requester_raw(raw_request(**same))
    # Another requester is another key; a finished row no longer holds this one.
    control_database.requester_raw(raw_request(**same, requested_by="'agent-other'"))
    control_database.requester_raw(
        f"UPDATE {APPROVALS} SET status = 'cancelled', closed_at = '{_stamp(datetime.now(UTC))}' "
        f"WHERE id = '{first}'"
    )
    control_database.requester_raw(raw_request(**same))


async def test_a_queue_refuses_a_schema_whose_index_was_dropped(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    control_database.raw(f"DROP INDEX {layout.OPEN_REQUEST_INDEX}")

    with pytest.raises(ConfigError, match=layout.OPEN_REQUEST_INDEX):
        await ask(split_queue(control_database))


def _duplicates(database: ControlDatabase, *, statuses: tuple[str, ...]) -> list[str]:
    """Drop the index and plant open duplicates, as a 0.1.0a4 database could hold."""
    database.raw(f"DROP INDEX {layout.OPEN_REQUEST_INDEX}")
    ids: list[str] = []
    base = datetime.now(UTC) - timedelta(minutes=30)
    for position, status in enumerate(statuses):
        request_id = str(uuid4())
        ids.append(request_id)
        decided = (
            f", decision = 'approve', resolved_by = 'user-17', resolved_at = '{_stamp(base)}'"
            if status == "approved"
            else ""
        )
        database.superuser_raw(
            "SET session_replication_role = replica; "
            + raw_request(
                id=f"'{request_id}'",
                payload_sha256="'" + "e" * 64 + "'",
                created_at=f"'{_stamp(base + timedelta(minutes=position))}'",
                expires_at=f"'{_stamp(base + timedelta(hours=2))}'",
            )
        )
        if decided:
            database.superuser_raw(
                "SET session_replication_role = replica; "
                f"UPDATE {APPROVALS} SET status = 'approved'{decided} WHERE id = '{request_id}'"
            )
    return ids


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _install(database: ControlDatabase, **options: Any) -> Any:
    assert database.owner_url is not None
    return install_postgres_schema(
        database.owner_url,
        requester_role=REQUESTER_ROLE,
        approver_role=APPROVER_ROLE,
        schema=database.schema,
        **options,
    )


def test_the_installer_refuses_existing_duplicates_and_changes_nothing(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("the installer is Postgres only")
    ids = _duplicates(control_database, statuses=("pending", "pending", "pending"))

    with pytest.raises(ConfigError, match="Nothing was changed") as raised:
        _install(control_database)

    assert all(request_id in str(raised.value) for request_id in ids)
    still_open = control_database.raw(f"SELECT count(*) FROM {APPROVALS} WHERE status = 'pending'")
    assert still_open == [(3,)]
    indexes = control_database.raw(
        f"SELECT 1 FROM pg_indexes WHERE indexname = '{layout.OPEN_REQUEST_INDEX}'"
    )
    assert indexes == []


def test_close_duplicates_keeps_the_oldest_pending_and_builds_the_index(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("the installer is Postgres only")
    first, second, third = _duplicates(control_database, statuses=("pending",) * 3)

    report = _install(control_database, close_duplicates=True)

    assert set(report.closed_duplicates) == {second, third}
    rows: dict[str, str] = dict(control_database.raw(f"SELECT id, status FROM {APPROVALS}"))
    assert rows == {first: "pending", second: "cancelled", third: "cancelled"}
    assert control_database.raw(
        f"SELECT 1 FROM pg_indexes WHERE indexname = '{layout.OPEN_REQUEST_INDEX}'"
    )
    # Idempotent: a second run has nothing to close.
    assert _install(control_database, close_duplicates=True).closed_duplicates == ()


def test_close_duplicates_keeps_the_approved_one_whatever_its_age(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("the installer is Postgres only")
    older, newer_approved = _duplicates(control_database, statuses=("pending", "approved"))

    report = _install(control_database, close_duplicates=True)

    assert report.closed_duplicates == (older,)
    rows: dict[str, str] = dict(control_database.raw(f"SELECT id, status FROM {APPROVALS}"))
    assert rows == {older: "cancelled", newer_approved: "approved"}


def test_two_approved_duplicates_are_a_decision_for_a_person(
    control_database: ControlDatabase,
) -> None:
    if control_database.owner_url is None:
        pytest.skip("the installer is Postgres only")
    _duplicates(control_database, statuses=("approved", "approved"))

    with pytest.raises(ConfigError, match="decision for a person"):
        _install(control_database, close_duplicates=True)
    assert control_database.raw(f"SELECT count(*) FROM {APPROVALS} WHERE status = 'approved'") == [
        (2,)
    ]


# SQLite: the library builds the index itself


async def test_sqlite_refuses_to_submit_over_duplicates_until_they_are_cancelled(
    control_database: ControlDatabase,
) -> None:
    if control_database.backend != "sqlite":
        pytest.skip("SQLite builds its own index")
    queue = split_queue(control_database)
    first = await ask(queue)
    control_database.raw(f"DROP INDEX {layout.OPEN_REQUEST_INDEX}")
    duplicate = str(uuid4())
    control_database.raw(
        raw_request(id=f"'{duplicate}'", payload_sha256=f"'{first.payload_sha256}'")
    )
    # A queue that has not looked yet, as after a restart on an a4 file.
    fresh = split_queue(control_database)

    with pytest.raises(ConfigError, match=duplicate):
        await ask(fresh)
    await fresh.cancel(first.id, principal=REQUESTER)  # reads and cancels still work
    again = await ask(fresh)

    assert again.id not in {first.id}
    assert control_database.raw(
        f"SELECT 1 FROM sqlite_master WHERE name = '{layout.OPEN_REQUEST_INDEX}'"
    )
