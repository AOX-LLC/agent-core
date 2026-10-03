"""Every column the requester role can write with plain SQL, and every reader of it.

The matrix: for each column, a hostile value is either refused by the database (the
bound) or accepted, and then every reader must cope with it: no crash, no hang, no
unaudited gap. A value the database accepts and a reader cannot read is the defect this
file exists to keep out. Postgres only: SQLite has no roles and no guard.
"""

import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from aox_agent_core.approvals import (
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
)
from aox_agent_core.approvals.sql import SQLApprovalQueue, approval_payload_hash
from aox_agent_core.approvals.types import ApprovalSide
from aox_agent_core.audit import AuditEvent
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import (
    ApprovalConflictError,
    ApprovalError,
    AuditIntegrityError,
    ConfigError,
    NotAuthorizedToResolveError,
)
from aox_agent_core.storage import (
    bind_approver_login,
    install_postgres_schema,
    open_database,
    unbind_approver_login,
)
from databases import (
    APPROVER_ROLE,
    REQUESTER_ROLE,
    TEST_ACTION_ROLES,
    ControlDatabase,
    approver_login,
    split_queue,
)
from test_approval_payload import ACTION, raw_request
from test_approvals import APPROVER, OTHER_APPROVER, REQUESTER
from test_audit_throughput import raw_insert

APPROVALS = "agent_core_approvals"
NOW = datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return f"'{canonical_timestamp(moment)}'"


def quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


LONG_ACTION = "a" + "b" * 99
LONG_NAME = "r" + "x" * 63
LONG_PRINCIPAL = "p" + "q" * 127
BIDI_SUMMARY = "Pay ‮elbaT‬ \x1b[31mred\x1b[0m ⁦x⁩ " + "y" * 450

# (id, session statements, column overrides): what a requester can try to insert.
APPROVAL_CASES: list[tuple[str, str, dict[str, str]]] = [
    ("action-at-100", "", {"action": quoted(LONG_ACTION)}),
    ("action-at-101", "", {"action": quoted(LONG_ACTION + "c")}),
    ("summary-500-with-bidi-and-ansi", "", {"summary": quoted(BIDI_SUMMARY[:500])}),
    ("summary-501", "", {"summary": quoted("s" * 501)}),
    ("requested-by-128", "", {"requested_by": quoted(LONG_PRINCIPAL)}),
    ("requested-by-with-at-sign", "", {"requested_by": quoted("a@b")}),
    ("required-role-64", "", {"required_role": quoted(LONG_NAME)}),
    ("required-role-65", "", {"required_role": quoted(LONG_NAME + "x")}),
    ("sha-all-zero", "", {"payload_sha256": quoted("0" * 64)}),
    ("sha-uppercase", "", {"payload_sha256": quoted("A" * 64)}),
    (
        "lifetime-exactly-168-hours",
        "",
        {
            "created_at": stamp(NOW - timedelta(minutes=1)),
            "expires_at": stamp(NOW - timedelta(minutes=1) + timedelta(hours=168)),
        },
    ),
    (
        "lifetime-168-hours-and-a-second",
        "",
        {
            "created_at": stamp(NOW - timedelta(minutes=1)),
            "expires_at": stamp(NOW - timedelta(minutes=1) + timedelta(hours=168, seconds=1)),
        },
    ),
    (
        # The writer's own time zone must not stretch "7 days": a fall-back shift makes a
        # local week 169 hours, which the model refuses to read.
        "lifetime-169-hours-in-new-york",
        "SET TimeZone = 'America/New_York'; ",
        {
            "created_at": stamp(datetime(2025, 10, 30, 12, tzinfo=UTC)),
            "expires_at": stamp(datetime(2025, 11, 6, 13, tzinfo=UTC)),
        },
    ),
    (
        "lifetime-169-hours-in-london",
        "SET TimeZone = 'Europe/London'; ",
        {
            "created_at": stamp(datetime(2025, 10, 23, 12, tzinfo=UTC)),
            "expires_at": stamp(datetime(2025, 10, 30, 13, tzinfo=UTC)),
        },
    ),
    (
        "created-in-the-far-past-already-due",
        "",
        {
            "created_at": stamp(datetime(2000, 1, 1, tzinfo=UTC)),
            "expires_at": stamp(datetime(2000, 1, 1, 1, tzinfo=UTC)),
        },
    ),
    (
        "created-four-minutes-ahead",
        "",
        {
            "created_at": stamp(NOW + timedelta(minutes=4)),
            "expires_at": stamp(NOW + timedelta(minutes=64)),
        },
    ),
    (
        "created-an-hour-ahead",
        "",
        {
            "created_at": stamp(NOW + timedelta(hours=1)),
            "expires_at": stamp(NOW + timedelta(hours=2)),
        },
    ),
    ("timestamp-24-hundred", "", {"created_at": quoted("2026-10-03T24:00:00.000000Z")}),
    ("timestamp-leap-second", "", {"created_at": quoted("2026-06-30T23:59:60.000000Z")}),
    (
        "delegates-16-duplicates-and-the-requester",
        "",
        {"delegates": quoted("[" + ",".join(['"agent-intake"'] * 16) + "]")},
    ),
    ("delegates-17", "", {"delegates": quoted("[" + ",".join(['"d"'] * 17) + "]")}),
    (
        "run-context-16-ids",
        "",
        {
            "run_context": quoted(
                '{"run_id":"r1","external_ids":{'
                + ",".join(f'"n{i}":"v{i}"' for i in range(16))
                + "}}"
            )
        },
    ),
    (
        "run-context-secret-shaped-name",
        "",
        {"run_context": quoted('{"run_id":"r1","external_ids":{"api_key":"abc"}}')},
    ),
    ("run-context-array", "", {"run_context": quoted("[1]")}),
    ("payload-json-array", "", {"payload_json": quoted("[1]")}),
    ("payload-json-lone-surrogate-escape", "", {"payload_json": quoted('{"a":"\\ud800"}')}),
    ("payload-json-duplicate-keys", "", {"payload_json": quoted('{"a":1,"a":2}')}),
]


async def readers_cope(queue: Any, request_id: Any) -> None:
    """Every reader either returns or raises an ApprovalError; none leaves the rest blocked."""
    await queue.list_pending(APPROVER)
    calls: tuple[Callable[[], Awaitable[Any]], ...] = (
        lambda: queue.get(request_id),
        lambda: queue.resolve(request_id, decision=Decision.REJECT, principal=APPROVER),
        lambda: queue.consume(request_id, action=ACTION, payload={}, principal=REQUESTER),
        lambda: queue.cancel(request_id, principal=REQUESTER),
    )
    for call in calls:
        with contextlib.suppress(ApprovalError):
            await call()


@pytest.mark.parametrize(
    ("statements", "columns"),
    [(statements, columns) for _, statements, columns in APPROVAL_CASES],
    ids=[case_id for case_id, _, _ in APPROVAL_CASES],
)
async def test_every_approvals_column_is_bounded_or_survivable(
    control_database: ControlDatabase, statements: str, columns: dict[str, str]
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    queue = split_queue(control_database)
    honest = await queue.submit(
        action=ACTION,
        summary="honest",
        payload={"n": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
        include_payload=True,
    )
    hostile_id = uuid4()
    try:
        control_database.requester_raw(statements + raw_request(id=f"'{hostile_id}'", **columns))
    except psycopg.Error:
        return  # the database's bound held: nothing to read

    started = time.monotonic()
    await readers_cope(queue, hostile_id)
    swept = await queue.expire_due(principal=REQUESTER)
    assert time.monotonic() - started < 10
    # Whatever it wrote, the honest request still reads, lists (if still pending) and expires.
    assert (await queue.get(honest.id)).id == honest.id
    assert swept >= 0
    status = control_database.raw(f"SELECT status FROM {APPROVALS} WHERE id = '{honest.id}'")
    assert status[0][0] in {s.value for s in ApprovalStatus}


async def test_a_request_the_database_let_in_never_blocks_the_sweep_for_the_others(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    queue = split_queue(control_database)
    due = {
        "created_at": stamp(NOW - timedelta(hours=3)),
        "expires_at": stamp(NOW - timedelta(hours=2)),
    }
    honest = await queue.submit(
        action=ACTION,
        summary="honest",
        payload={},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
    )
    for _ in range(3):
        control_database.requester_raw(raw_request(**due))
    control_database.requester_raw(raw_request(**due, run_context=quoted('{"run_id":"x"}')))

    assert await queue.expire_due(principal=REQUESTER) == 4
    assert (await queue.get(honest.id)).status is ApprovalStatus.PENDING


# The audit table


def audit_row(seq: int, prev_hash: str, **columns: str) -> str:
    return raw_insert(seq, prev_hash, **columns)


AUDIT_CASES: list[tuple[str, dict[str, str], bool]] = [
    # (id, columns, the database should refuse it)
    ("action-101", {"action": quoted("a" * 101)}, True),
    ("action-uppercase", {"action": quoted("Model.Call")}, True),
    ("actor-129", {"actor_id": quoted("a" * 129)}, True),
    ("actor-with-at-sign", {"actor_id": quoted("a@b")}, True),
    ("subject-201", {"subject_id": quoted("s" * 201)}, True),
    ("payload-over-8192", {"payload": quoted('{"a":"' + "x" * 8_200 + '"}')}, True),
    ("payload-array", {"payload": quoted("[1]")}, True),
    ("payload-not-json", {"payload": quoted("not json")}, True),
    ("run-context-over-2048", {"run_context": quoted('{"a":"' + "x" * 2_100 + '"}')}, True),
    ("run-context-array", {"run_context": quoted("[1]")}, True),
    ("payload-at-the-limit", {"payload": quoted('{"a":"' + "x" * 8_150 + '"}')}, False),
    ("payload-infinity", {"payload": quoted('{"a":1e400}')}, False),
    ("payload-deep", {"payload": quoted('{"a":' + "[" * 1_500 + "]" * 1_500 + "}")}, False),
    ("actor-at-128", {"actor_id": quoted("a" * 128)}, False),
    ("subject-at-200", {"subject_id": quoted("s" * 200)}, False),
]


@pytest.mark.parametrize(
    ("columns", "refused"),
    [(columns, refused) for _, columns, refused in AUDIT_CASES],
    ids=[case_id for case_id, _, _ in AUDIT_CASES],
)
async def test_every_audit_column_is_bounded_or_survivable(
    control_database: ControlDatabase, columns: dict[str, str], refused: bool
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    log = SQLAuditLog(control_database.database)
    first = await log.append(AuditEvent(action="model.call", actor_id="svc"))

    try:
        control_database.requester_raw(audit_row(2, first.record_hash, **columns))
    except psycopg.Error:
        assert refused, "the database refused a value the matrix expects it to accept"
        assert (await log.verify()).seq == 1
        return
    assert not refused, "the database accepted a value the matrix expects it to refuse"

    # Accepted: it has a wrong hash, so verify() reports it by name, and every other reader
    # keeps working: none crashes with anything but the library's own error, none hangs.
    started = time.monotonic()
    head = await log.head()
    if _unparsable(columns):
        with pytest.raises(AuditIntegrityError, match="Record 2"):
            _ = [r async for r in log.iter_records()]
    else:
        assert [r.seq async for r in log.iter_records()] == [1, 2]
    with pytest.raises(AuditIntegrityError, match="Record 2"):
        await log.verify()
    assert time.monotonic() - started < 10
    assert head.seq == 2


def _unparsable(columns: dict[str, str]) -> bool:
    payload = columns.get("payload", "")
    return "[" * 100 in payload  # json.loads recurses too deeply: a malformed record


# The approver's own column: when a decision was made.

RESOLVED_AT_CASES: list[tuple[str, timedelta, timedelta, bool]] = [
    # (id, created_at relative to now, resolved_at relative to now, the database accepts it)
    ("now", timedelta(minutes=-1), timedelta(0), True),
    ("at-created-at", timedelta(minutes=-1), timedelta(minutes=-1), True),
    ("four-minutes-ahead", timedelta(minutes=-1), timedelta(minutes=4), True),
    ("before-created-at", timedelta(0), timedelta(minutes=-1), False),
    ("ten-minutes-ahead", timedelta(minutes=-1), timedelta(minutes=10), False),
    ("a-year-ahead", timedelta(minutes=-1), timedelta(days=365), False),
    ("ten-minutes-ago-though-after-created-at", timedelta(hours=-1), timedelta(minutes=-10), False),
]


@pytest.mark.parametrize(
    ("created", "resolved", "accepted"),
    [(created, resolved, accepted) for _, created, resolved, accepted in RESOLVED_AT_CASES],
    ids=[case_id for case_id, *_ in RESOLVED_AT_CASES],
)
async def test_the_approver_cannot_date_a_decision_outside_the_requests_life_and_the_clock(
    control_database: ControlDatabase, created: timedelta, resolved: timedelta, accepted: bool
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    now = datetime.now(UTC)
    row = uuid4()
    control_database.requester_raw(
        raw_request(
            id=f"'{row}'",
            created_at=stamp(now + created),
            expires_at=stamp(now + created + timedelta(hours=2)),
        )
    )
    decide = (
        f"UPDATE {APPROVALS} SET status = 'approved', decision = 'approve', "
        f"resolved_by = 'user-17', resolved_at = {stamp(now + resolved)} WHERE id = '{row}'"
    )

    if accepted:
        control_database.approver_raw(decide)
        queue = split_queue(control_database)
        read = await queue.get(row)
        assert read.status is ApprovalStatus.APPROVED
        assert read.resolved_at is not None
        assert read.resolved_at >= read.created_at
    else:
        with pytest.raises(psycopg.Error):
            control_database.approver_raw(decide)
        status = control_database.raw(f"SELECT status FROM {APPROVALS} WHERE id = '{row}'")
        assert status == [("pending",)]


# The unique index on (requested_by, action, payload_sha256) for open requests.


async def test_a_requester_that_occupies_anothers_key_is_survivable(
    control_database: ControlDatabase,
) -> None:
    """requested_by is written by the requester role, so it can hold another principal's key.

    The principal then meets a conflict that names the request, which it may cancel as the
    requester it is, and its next submit goes through.
    """
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    victim = Principal(id="agent-billing", kind=PrincipalKind.AGENT)
    queue = split_queue(control_database)
    payload = {"contact_id": "c-1001"}
    squatter = uuid4()
    control_database.requester_raw(
        raw_request(
            id=f"'{squatter}'",
            requested_by=quoted(victim.id),
            required_role=quoted("some.other_role"),
            payload_sha256=quoted(approval_payload_hash(ACTION, payload)),
        )
    )
    terms: dict[str, Any] = {
        "action": ACTION,
        "summary": "mine",
        "payload": payload,
        "requested_by": victim,
        "required_role": "ops.approver",
        "ttl_seconds": 3_600,
    }

    with pytest.raises(ApprovalConflictError) as raised:
        await queue.submit(**terms)
    assert raised.value.existing == squatter
    await queue.cancel(squatter, principal=victim)

    mine = await queue.submit(**terms)
    assert mine.id != squatter
    assert (await queue.get(mine.id)).status is ApprovalStatus.PENDING


async def test_the_index_holds_for_every_open_status_and_lets_finished_rows_go(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    sha = quoted("f" * 64)
    first = uuid4()
    control_database.requester_raw(raw_request(id=f"'{first}'", payload_sha256=sha))

    with pytest.raises(psycopg.errors.UniqueViolation):
        control_database.requester_raw(raw_request(payload_sha256=sha))
    control_database.requester_raw(
        f"UPDATE {APPROVALS} SET status = 'cancelled', closed_at = {stamp(NOW)} "
        f"WHERE id = '{first}'"
    )
    control_database.requester_raw(raw_request(payload_sha256=sha))  # the key is free again


# payload_json and payload_purged_at: only the approver role purges, only a finished row, only
# after the retention floor, and nothing else changes with it.


def payload_row(
    control_database: ControlDatabase, status: str, *, aged: timedelta | None = None
) -> str:
    """A request in `status` holding a payload, planted past the guard, finished `aged` ago."""
    request_id = str(uuid4())
    moment = datetime.now(UTC) - (aged or timedelta(0))
    created = moment - timedelta(hours=1)
    columns: dict[str, str] = {
        "id": request_id,
        "action": ACTION,
        "summary": "s",
        "payload_sha256": uuid4().hex * 2,
        "requested_by": "agent-intake",
        "required_role": "ops.approver",
        "created_at": canonical_timestamp(created),
        "expires_at": canonical_timestamp(created + timedelta(hours=2)),
        "status": status,
        "payload_json": '{"a":1}',
    }
    if status in {"approved", "rejected", "consumed"}:
        columns |= {
            "decision": "reject" if status == "rejected" else "approve",
            "resolved_by": "user-17",
            "resolved_at": canonical_timestamp(moment),
        }
    if status == "consumed":
        columns["consumed_at"] = canonical_timestamp(moment)
    if status in {"cancelled", "expired"}:
        columns["closed_at"] = canonical_timestamp(moment)
    values = ", ".join(quoted(value) for value in columns.values())
    control_database.superuser_raw(
        "SET session_replication_role = replica; "
        f"INSERT INTO {APPROVALS} ({', '.join(columns)}) VALUES ({values})"
    )
    return request_id


def purge_sql(request_id: str, *, also: str = "") -> str:
    purged_at = stamp(datetime.now(UTC))
    return (
        f"UPDATE {APPROVALS} SET payload_json = NULL, payload_purged_at = {purged_at}"
        f"{also} WHERE id = '{request_id}'"
    )


def refused(run: Callable[[str], Any], sql: str) -> bool:
    try:
        run(sql)
    except psycopg.Error:
        return True
    return False


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_nobody_can_null_the_payload_of_a_request_still_open(
    control_database: ControlDatabase, status: str
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    row = payload_row(control_database, status, aged=timedelta(days=30))

    assert refused(control_database.requester_raw, purge_sql(row))
    assert refused(
        control_database.requester_raw,
        f"UPDATE {APPROVALS} SET payload_json = NULL WHERE id = '{row}'",
    )
    assert refused(control_database.approver_raw, purge_sql(row))
    assert refused(
        control_database.approver_raw,
        f"UPDATE {APPROVALS} SET payload_json = NULL WHERE id = '{row}'",
    )
    kept = control_database.raw(f"SELECT payload_json FROM {APPROVALS} WHERE id = '{row}'")
    assert kept == [('{"a":1}',)]


@pytest.mark.parametrize("status", ["consumed", "rejected", "cancelled", "expired"])
def test_the_approver_purges_a_finished_request_past_the_floor_and_nobody_else_does(
    control_database: ControlDatabase, status: str
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    row = payload_row(control_database, status, aged=timedelta(days=3))

    assert refused(control_database.requester_raw, purge_sql(row))
    control_database.approver_raw(purge_sql(row))

    after = control_database.raw(
        f"SELECT payload_json, payload_purged_at IS NOT NULL, status FROM {APPROVALS} "
        f"WHERE id = '{row}'"
    )
    assert after == [(None, True, status)]


@pytest.mark.parametrize("status", ["consumed", "rejected", "cancelled", "expired"])
def test_a_purge_inside_the_retention_floor_is_refused(
    control_database: ControlDatabase, status: str
) -> None:
    if control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    just_now = payload_row(control_database, status)
    an_hour = payload_row(control_database, status, aged=timedelta(hours=1))

    assert refused(control_database.approver_raw, purge_sql(just_now))
    assert refused(control_database.approver_raw, purge_sql(an_hour))
    kept = control_database.raw(f"SELECT count(*) FROM {APPROVALS} WHERE payload_json IS NOT NULL")
    assert kept == [(2,)]


def test_a_purge_that_changes_anything_else_is_refused(
    control_database: ControlDatabase,
) -> None:
    if control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    row = payload_row(control_database, "consumed", aged=timedelta(days=3))

    for also in (", reason = 'because'", ", status = 'cancelled'", ", payload_sha256 = 'f'"):
        assert refused(control_database.approver_raw, purge_sql(row, also=also)), also
    # A payload_purged_at that is not a canonical timestamp is refused too.
    garbled = f"UPDATE {APPROVALS} SET payload_json = NULL, payload_purged_at = 'yesterday'"
    assert refused(control_database.approver_raw, f"{garbled} WHERE id = '{row}'")
    control_database.approver_raw(purge_sql(row))


def test_the_marker_cannot_be_forged_set_without_a_purge_or_undone(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    stored = payload_row(control_database, "cancelled", aged=timedelta(days=3))
    never = str(uuid4())
    control_database.superuser_raw(
        "SET session_replication_role = replica; "
        + raw_request(
            id=quoted(never), status=quoted("cancelled"), closed_at=stamp(NOW - timedelta(days=3))
        )
    )
    mark = f"UPDATE {APPROVALS} SET payload_purged_at = {stamp(datetime.now(UTC))}"

    # Marked without purging, or on a request that never stored a payload.
    assert refused(control_database.approver_raw, f"{mark} WHERE id = '{stored}'")
    assert refused(control_database.approver_raw, f"{mark} WHERE id = '{never}'")
    assert refused(control_database.requester_raw, f"{mark} WHERE id = '{stored}'")
    # A request cannot be inserted already purged.
    assert refused(
        control_database.requester_raw, raw_request(payload_purged_at=stamp(datetime.now(UTC)))
    )
    # Purged is permanent: the payload cannot be put back, nor the mark cleared.
    control_database.approver_raw(purge_sql(stored))
    assert refused(
        control_database.approver_raw,
        f"UPDATE {APPROVALS} SET payload_json = '{{\"a\":1}}' WHERE id = '{stored}'",
    )
    assert refused(
        control_database.approver_raw,
        f"UPDATE {APPROVALS} SET payload_purged_at = NULL WHERE id = '{stored}'",
    )
    assert refused(control_database.approver_raw, purge_sql(stored))


async def test_a_queue_refuses_a_requester_role_that_can_write_the_payload_columns(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    control_database.superuser_raw(
        f"GRANT UPDATE (payload_json) ON {APPROVALS} TO agent_core_requester"
    )

    with pytest.raises(ConfigError, match="payload_json"):
        await split_queue(control_database).submit(
            action=ACTION,
            summary="s",
            payload={},
            requested_by=REQUESTER,
            required_role="ops.approver",
            ttl_seconds=60,
        )


# A finish time the client writes cannot get a payload purged early: the guard writes it.


def _finished_at(database: ControlDatabase, row: str, column: str) -> datetime:
    value = database.raw(f"SELECT {column} FROM {APPROVALS} WHERE id = '{row}'")[0][0]
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "claimed", [datetime(2000, 1, 1, tzinfo=UTC), datetime(2099, 1, 1, tzinfo=UTC)]
)
def test_a_cancel_or_close_is_stamped_by_the_database_whatever_the_client_wrote(
    control_database: ControlDatabase, claimed: datetime
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    cancelled = str(uuid4())
    control_database.requester_raw(
        raw_request(id=quoted(cancelled), payload_json=quoted('{"a":1}'))
    )
    control_database.requester_raw(
        f"UPDATE {APPROVALS} SET status = 'cancelled', closed_at = {stamp(claimed)} "
        f"WHERE id = '{cancelled}'"
    )

    stored = _finished_at(control_database, cancelled, "closed_at")
    assert abs(datetime.now(UTC) - stored) < timedelta(minutes=1)
    # So the approver's purge, which waits out the floor, is refused.
    assert refused(control_database.approver_raw, purge_sql(cancelled))


def test_an_expiry_by_the_approver_is_stamped_by_the_database_too(
    control_database: ControlDatabase,
) -> None:
    if control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    lapsed = str(uuid4())
    created = datetime.now(UTC) - timedelta(hours=3)
    control_database.superuser_raw(
        "SET session_replication_role = replica; "
        + raw_request(
            id=quoted(lapsed),
            payload_json=quoted('{"a":1}'),
            created_at=stamp(created),
            expires_at=stamp(created + timedelta(hours=1)),
        )
    )

    backdated = stamp(datetime(2000, 1, 1, tzinfo=UTC))
    control_database.approver_raw(
        f"UPDATE {APPROVALS} SET status = 'expired', closed_at = {backdated} WHERE id = '{lapsed}'"
    )

    assert abs(datetime.now(UTC) - _finished_at(control_database, lapsed, "closed_at")) < timedelta(
        minutes=1
    )
    assert refused(control_database.approver_raw, purge_sql(lapsed))


def test_a_consume_is_stamped_by_the_database_whatever_the_client_wrote(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    approved = payload_row(control_database, "approved")
    control_database.requester_raw(
        f"UPDATE {APPROVALS} SET status = 'consumed', "
        f"consumed_at = {stamp(datetime(2000, 1, 1, tzinfo=UTC))} WHERE id = '{approved}'"
    )

    stored = _finished_at(control_database, approved, "consumed_at")
    assert abs(datetime.now(UTC) - stored) < timedelta(minutes=1)
    assert refused(control_database.approver_raw, purge_sql(approved))


@pytest.mark.parametrize(
    "claimed", [datetime(2000, 1, 1, tzinfo=UTC), datetime(2099, 1, 1, tzinfo=UTC)]
)
def test_a_purge_is_stamped_by_the_database_whatever_the_client_wrote(
    control_database: ControlDatabase, claimed: datetime
) -> None:
    if control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    row = payload_row(control_database, "cancelled", aged=timedelta(days=3))

    control_database.approver_raw(
        f"UPDATE {APPROVALS} SET payload_json = NULL, payload_purged_at = {stamp(claimed)} "
        f"WHERE id = '{row}'"
    )

    stored = _finished_at(control_database, row, "payload_purged_at")
    assert abs(datetime.now(UTC) - stored) < timedelta(minutes=1)


@pytest.mark.parametrize("claimed_ago", [timedelta(minutes=4), timedelta(0)])
def test_a_rejection_is_stamped_by_the_database_whatever_the_client_wrote(
    control_database: ControlDatabase, claimed_ago: timedelta
) -> None:
    """A rejection's finish time is its resolved_at, which the retention floor counts from."""
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    pending = str(uuid4())
    started = {
        "created_at": stamp(datetime.now(UTC) - timedelta(minutes=10)),
        "expires_at": stamp(datetime.now(UTC) + timedelta(hours=1)),
    }
    control_database.requester_raw(
        raw_request(id=quoted(pending), payload_json=quoted('{"a":1}'), **started)
    )

    control_database.approver_raw(
        f"UPDATE {APPROVALS} SET status = 'rejected', decision = 'reject', "
        f"resolved_by = 'user-17', resolved_at = {stamp(datetime.now(UTC) - claimed_ago)} "
        f"WHERE id = '{pending}'"
    )

    stored = _finished_at(control_database, pending, "resolved_at")
    assert abs(datetime.now(UTC) - stored) < timedelta(seconds=30)
    assert stored >= _finished_at(control_database, pending, "created_at")
    # An approval is not a finish time: its resolved_at stays as the approver wrote it.
    approved = str(uuid4())
    control_database.requester_raw(raw_request(id=quoted(approved), **started))
    claimed = datetime.now(UTC) - timedelta(minutes=4)
    control_database.approver_raw(
        f"UPDATE {APPROVALS} SET status = 'approved', decision = 'approve', "
        f"resolved_by = 'user-17', resolved_at = {stamp(claimed)} WHERE id = '{approved}'"
    )
    kept = _finished_at(control_database, approved, "resolved_at")
    assert abs(kept - claimed) < timedelta(seconds=1)


async def test_a_rejection_of_a_request_dated_ahead_is_stamped_no_earlier_than_its_creation(
    control_database: ControlDatabase,
) -> None:
    """The guard allows created_at up to 5 minutes ahead; the library will not read a decision
    dated before its request, so the stamp is GREATEST(database clock, created_at)."""
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    ahead = datetime.now(UTC) + timedelta(minutes=4)
    row = str(uuid4())
    control_database.requester_raw(
        raw_request(
            id=quoted(row),
            created_at=stamp(ahead),
            expires_at=stamp(ahead + timedelta(hours=1)),
        )
    )

    control_database.approver_raw(
        f"UPDATE {APPROVALS} SET status = 'rejected', decision = 'reject', "
        f"resolved_by = 'user-17', resolved_at = {stamp(ahead + timedelta(seconds=1))} "
        f"WHERE id = '{row}'"
    )

    assert _finished_at(control_database, row, "resolved_at") >= _finished_at(
        control_database, row, "created_at"
    )
    read = await split_queue(control_database).get(UUID(row))
    assert read.status is ApprovalStatus.REJECTED


async def test_resolve_returns_the_resolved_at_the_database_stored(
    control_database: ControlDatabase,
) -> None:
    """An application clock 3 minutes ahead (the guard allows 5): a rejection is stored with the
    database's time, and the request resolve returns says the same."""
    if control_database.approver_raw is None:
        pytest.skip("the guard stamps a rejection on Postgres only")
    submitter = split_queue(control_database)
    queue = split_queue(control_database, clock=lambda: datetime.now(UTC) + timedelta(minutes=3))
    request = await submitter.submit(
        action=ACTION,
        summary="s",
        payload={"a": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )

    rejected = await queue.resolve(request.id, decision=Decision.REJECT, principal=APPROVER)

    stored = _finished_at(control_database, str(request.id), "resolved_at")
    assert rejected.resolved_at == stored
    assert abs(datetime.now(UTC) - stored) < timedelta(seconds=30)


# Login binding: with it on, resolved_by must be the principal the owner mapped to the login
# that authenticated (session_user). SET ROLE changes current_user and never session_user.


def bind_logins(database: ControlDatabase, *mappings: tuple[str, str], on: bool = True) -> None:
    """Map each (login, principal) as the owner, then install with binding `on`."""
    assert database.owner_url is not None
    for login, principal in mappings:
        bind_approver_login(
            database.owner_url, login=login, principal=principal, schema=database.schema
        )
    install_postgres_schema(
        database.owner_url,
        requester_role=REQUESTER_ROLE,
        approver_role=APPROVER_ROLE,
        schema=database.schema,
        bind_resolved_by=on,
    )


def pending_row(database: ControlDatabase) -> str:
    assert database.requester_raw is not None
    row = str(uuid4())
    database.requester_raw(raw_request(id=f"'{row}'"))
    return row


def decide_sql(row: str, principal: str, *, status: str = "approved") -> str:
    decision = "approve" if status == "approved" else "reject"
    return (
        f"UPDATE {APPROVALS} SET status = '{status}', decision = '{decision}', "
        f"resolved_by = '{principal}', resolved_at = {stamp(datetime.now(UTC))} "
        f"WHERE id = '{row}'"
    )


def status_of(database: ControlDatabase, row: str) -> str:
    return str(database.raw(f"SELECT status FROM {APPROVALS} WHERE id = '{row}'")[0][0])


def runner_as(url: str, *statements: str) -> None:
    """Run `statements` in one session on `url`, so SET ROLE carries over."""
    with psycopg.connect(url, autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement)


MISMATCH = "mapped to the deciding login"


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_a_bound_login_records_only_its_own_principal(
    control_database: ControlDatabase, status: str
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("logins and the guard exist only on Postgres")
    login, url = approver_login(control_database)
    other, _ = approver_login(control_database)
    bind_logins(control_database, (login, "user-17"), (other, "user-23"))
    row = pending_row(control_database)

    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, decide_sql(row, "user-23", status=status))
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, decide_sql(row, "user-99", status=status))
    assert status_of(control_database, row) == "pending"

    runner_as(url, decide_sql(row, "user-17", status=status))
    assert status_of(control_database, row) == status


def test_binding_leaves_the_requester_role_no_route_to_approved(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("logins and the guard exist only on Postgres")
    login, _ = approver_login(control_database)
    bind_logins(control_database, (login, "user-17"))
    row = pending_row(control_database)

    with pytest.raises(psycopg.Error):
        control_database.requester_raw(decide_sql(row, "user-17"))
    with pytest.raises(psycopg.Error):
        control_database.requester_raw(
            f"UPDATE {APPROVALS} SET status = 'approved' WHERE id = '{row}'"
        )
    assert status_of(control_database, row) == "pending"


def test_an_approver_login_with_no_mapping_cannot_decide_while_binding_is_on(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("logins and the guard exist only on Postgres")
    mapped, _ = approver_login(control_database)
    unmapped, url = approver_login(control_database)
    bind_logins(control_database, (mapped, "user-17"))
    row = pending_row(control_database)

    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, decide_sql(row, "user-17"))
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, decide_sql(row, unmapped))
    assert status_of(control_database, row) == "pending"


def test_with_binding_off_a_login_may_record_any_principal(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("logins and the guard exist only on Postgres")
    login, url = approver_login(control_database)
    bind_logins(control_database, (login, "user-17"), on=False)
    row = pending_row(control_database)

    runner_as(url, decide_sql(row, "user-23"))  # a6's behaviour: nothing binds it

    assert status_of(control_database, row) == "approved"


def test_a_member_who_logs_in_as_themselves_then_switches_role_is_judged_as_themselves(
    control_database: ControlDatabase,
) -> None:
    """session_user is the identity: SET ROLE to another approver's login does not lend it."""
    if control_database.superuser_url is None:
        pytest.skip("logins and the guard exist only on Postgres")
    victim, _ = approver_login(control_database)
    # `mine` can switch to `victim`: it is a member of it, as a person who may act for them.
    mine, url = approver_login(control_database, also_member_of=(victim,))
    bind_logins(control_database, (mine, "user-17"), (victim, "user-23"))
    switched, own = pending_row(control_database), pending_row(control_database)

    # Logged in as itself, then SET ROLE victim: current_user is victim, session_user is not.
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, f"SET ROLE {victim}", decide_sql(switched, "user-23"))
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, f"SET ROLE {APPROVER_ROLE}", decide_sql(switched, "user-23"))
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, f"SET ROLE {APPROVER_ROLE}", decide_sql(switched, "user-99"))
    assert status_of(control_database, switched) == "pending"

    # Its own principal still goes through under a switched role, and the audit row written
    # in that session names both: db_role is who it acted as, db_login who really did.
    runner_as(
        url,
        f"SET ROLE {victim}",
        decide_sql(own, "user-17"),
        raw_insert(1, "0" * 64, db_login="'forged'"),
    )
    assert status_of(control_database, own) == "approved"
    assert control_database.raw("SELECT db_role, db_login FROM agent_core_audit") == [
        (victim, mine)
    ]


def test_the_audit_login_is_the_session_login_whatever_the_writer_supplies(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the audit trigger exists only on Postgres")

    control_database.requester_raw(raw_insert(1, "0" * 64, db_login="'someone-else'"))
    head = control_database.raw("SELECT record_hash FROM agent_core_audit")[0][0]
    control_database.approver_raw(
        raw_insert(2, head, db_login="NULL", db_role="'agent_core_requester'")
    )

    assert control_database.raw(
        "SELECT seq, db_role, db_login FROM agent_core_audit ORDER BY seq"
    ) == [(1, REQUESTER_ROLE, REQUESTER_ROLE), (2, APPROVER_ROLE, APPROVER_ROLE)]


async def test_db_login_is_outside_the_hash_and_a_record_without_it_verifies(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)
    first = await log.append(AuditEvent(action="model.call", actor_id="svc"))

    assert first.schema_version == 4
    if control_database.backend == "postgres":
        assert first.db_login == REQUESTER_ROLE
    else:
        assert first.db_login is None
    assert (await log.verify()).seq == 1


def test_only_the_owner_can_touch_the_login_mapping(control_database: ControlDatabase) -> None:
    if control_database.superuser_url is None:
        pytest.skip("the mapping table exists only on Postgres")
    login, _ = approver_login(control_database)
    bind_logins(control_database, (login, "user-17"))
    table = "agent_core_approver_logins"
    assert control_database.requester_raw is not None
    assert control_database.approver_raw is not None

    for run in (control_database.requester_raw, control_database.approver_raw):
        for statement in (
            f"SELECT * FROM {table}",
            f"INSERT INTO {table} (login, principal) VALUES ('x', 'user-99')",
            f"UPDATE {table} SET principal = 'user-99'",
            f"UPDATE {table} SET removed_at = now()",
            f"DELETE FROM {table}",
            f"TRUNCATE {table}",
        ):
            with pytest.raises(psycopg.Error, match="permission denied"):
                run(statement)
    with pytest.raises(psycopg.Error, match="permission denied"):
        runner_as(control_database.url.replace(REQUESTER_ROLE, login), f"SELECT * FROM {table}")
    assert control_database.raw(f"SELECT login, principal FROM {table}") == [(login, "user-17")]


def test_the_owner_cannot_delete_rewrite_or_reuse_a_mapping(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("the mapping table exists only on Postgres")
    first, _ = approver_login(control_database)
    second, _ = approver_login(control_database)
    bind_logins(control_database, (first, "user-17"))
    table = "agent_core_approver_logins"
    owner = control_database.raw

    for statement in (
        f"DELETE FROM {table}",
        f"TRUNCATE {table}",
        f"UPDATE {table} SET principal = 'user-23'",
        f"UPDATE {table} SET login = '{second}'",
        f"INSERT INTO {table} (login, principal) VALUES ('{second}', 'user-17')",  # principal twice
        f"INSERT INTO {table} (login, principal) VALUES ('{first}', 'user-23')",  # login twice
        f"INSERT INTO {table} (login, principal) VALUES ('{second}', 'bad principal!')",
    ):
        with pytest.raises(psycopg.Error):
            owner(statement)

    unbind_approver_login(
        control_database.owner_url or "", login=first, schema=control_database.schema
    )
    removed = owner(f"SELECT removed_at IS NOT NULL FROM {table} WHERE login = '{first}'")
    assert removed == [(True,)]
    # A removed mapping is final: neither the login nor the principal comes back.
    with pytest.raises(psycopg.Error):
        owner(f"UPDATE {table} SET removed_at = NULL")
    with pytest.raises(ConfigError, match="never mapped twice"):
        bind_approver_login(
            control_database.owner_url or "",
            login=first,
            principal="user-55",
            schema=control_database.schema,
        )
    with pytest.raises(ConfigError, match="never mapped twice"):
        bind_approver_login(
            control_database.owner_url or "",
            login=second,
            principal="user-17",
            schema=control_database.schema,
        )
    # and the removed login can no longer decide.
    row = pending_row(control_database)
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(control_database.url.replace(REQUESTER_ROLE, first), decide_sql(row, "user-17"))


def test_a_login_must_be_an_approver_login_to_be_mapped(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("the mapping table exists only on Postgres")
    assert control_database.owner_url is not None
    plain = f"agent_core_login_{uuid4().hex[:8]}"
    control_database.superuser_raw(f"CREATE ROLE {plain} LOGIN")
    control_database.roles.append(plain)
    both, _ = approver_login(control_database, also_member_of=(REQUESTER_ROLE,))

    for login in (plain, both, "no_such_role", REQUESTER_ROLE):
        with pytest.raises(ConfigError):
            bind_approver_login(
                control_database.owner_url,
                login=login,
                principal="user-17",
                schema=control_database.schema,
            )
    with pytest.raises(ConfigError, match="not a principal id"):
        bind_approver_login(
            control_database.owner_url, login=both, principal="a b", schema=control_database.schema
        )


def test_the_installer_keeps_binding_unless_told_and_lists_unmapped_logins(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("the installer is Postgres only")
    assert control_database.owner_url is not None
    mapped, _ = approver_login(control_database)
    unmapped, _ = approver_login(control_database)

    def install(**options: Any) -> Any:
        return install_postgres_schema(
            control_database.owner_url or "",
            requester_role=REQUESTER_ROLE,
            approver_role=APPROVER_ROLE,
            schema=control_database.schema,
            **options,
        )

    assert install().login_binding is False  # off in a fresh install
    bind_approver_login(
        control_database.owner_url,
        login=mapped,
        principal="user-17",
        schema=control_database.schema,
    )
    report = install(bind_resolved_by=True)
    assert report.login_binding is True
    assert unmapped in report.unmapped_logins
    assert mapped not in report.unmapped_logins
    assert unmapped in str(report)
    # A reinstall that does not mention binding must not switch it off.
    assert install().login_binding is True
    assert install(bind_resolved_by=False).login_binding is False


async def test_a_queue_refuses_to_start_when_binding_is_on_and_the_mapping_is_missing(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("login binding exists only on Postgres")
    login, url = approver_login(control_database)
    unmapped, unmapped_url = approver_login(control_database)
    bind_logins(control_database, (login, "user-17"))

    def approver_queue(database_url: str) -> SQLApprovalQueue:
        database = open_database(database_url)
        schema = control_database.schema
        return SQLApprovalQueue(
            database, audit_log=SQLAuditLog(database, schema=schema), schema=schema
        )

    # The mapped login starts; the requester side needs no mapping.
    assert await approver_queue(url).side() is ApprovalSide.APPROVER
    assert await split_queue(control_database).requester.side() is ApprovalSide.REQUESTER
    # An approver login with no mapping does not.
    with pytest.raises(ConfigError, match=f"{unmapped} has no principal mapped"):
        await approver_queue(unmapped_url).side()
    # Nor does any queue once the mapping table is gone or can be written by a runtime role.
    control_database.superuser_raw(f"GRANT INSERT ON agent_core_approver_logins TO {APPROVER_ROLE}")
    with pytest.raises(ConfigError, match="can write the login mapping table"):
        await approver_queue(url).side()
    control_database.superuser_raw(
        f"REVOKE INSERT ON agent_core_approver_logins FROM {APPROVER_ROLE}"
    )
    control_database.superuser_raw("DROP TABLE agent_core_approver_logins CASCADE")
    with pytest.raises(ConfigError, match="login mapping is missing or unprotected"):
        await approver_queue(url).side()


async def test_a_bound_queue_resolves_as_its_login_and_refuses_another_principal_with_an_audit(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("login binding exists only on Postgres")
    login, url = approver_login(control_database)
    bind_logins(control_database, (login, APPROVER.id))
    schema = control_database.schema
    database = open_database(url)
    approver_side = SQLApprovalQueue(
        database,
        audit_log=SQLAuditLog(database, schema=schema),
        schema=schema,
        policy=RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES),
    )
    request = await split_queue(control_database).submit(
        action=ACTION,
        summary="s",
        payload={"a": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )

    with pytest.raises(NotAuthorizedToResolveError, match="only the principal the owner mapped"):
        await approver_side.resolve(request.id, decision=Decision.APPROVE, principal=OTHER_APPROVER)
    assert status_of(control_database, str(request.id)) == "pending"
    resolved = await approver_side.resolve(
        request.id, decision=Decision.APPROVE, principal=APPROVER
    )

    assert resolved.resolved_by == APPROVER.id
    log = SQLAuditLog(control_database.database, schema=schema)
    records = [record async for record in log.iter_records()]
    denied = [r for r in records if r.action == "approval.resolve_denied"]
    assert [r.payload["reason"] for r in denied] == ["login_binding"]
    assert {r.db_login for r in records if r.action.startswith("approval.resolve")} == {login}
    assert (await log.verify()).seq == len(records)


def test_a_role_dropped_and_created_again_under_the_same_name_inherits_no_mapping(
    control_database: ControlDatabase,
) -> None:
    """The mapping is bound to the role's OID, not only its name."""
    if control_database.superuser_url is None:
        pytest.skip("logins and the guard exist only on Postgres")
    login, _ = approver_login(control_database)
    bind_logins(control_database, (login, "user-17"))
    control_database.superuser_raw(f"DROP ROLE {login}")
    control_database.superuser_raw(f"CREATE ROLE {login} LOGIN INHERIT")
    control_database.superuser_raw(f"GRANT {APPROVER_ROLE} TO {login}")
    assert control_database.superuser_url is not None
    url = control_database.superuser_url.replace("postgres@", f"{login}@", 1)
    row = pending_row(control_database)

    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(url, decide_sql(row, "user-17"))
    assert status_of(control_database, row) == "pending"
    # and the name cannot be mapped again either: a login is never reused.
    with pytest.raises(ConfigError, match="never mapped twice"):
        bind_approver_login(
            control_database.owner_url or "",
            login=login,
            principal="user-31",
            schema=control_database.schema,
        )


def test_renaming_roles_cannot_move_a_mapping_to_another_login(
    control_database: ControlDatabase,
) -> None:
    if control_database.superuser_url is None:
        pytest.skip("logins and the guard exist only on Postgres")
    mapped, mapped_url = approver_login(control_database)
    other, other_url = approver_login(control_database)
    bind_logins(control_database, (mapped, "user-17"))
    # Move the name: the old role goes away under a new name, another role takes its name.
    control_database.superuser_raw(f"ALTER ROLE {mapped} RENAME TO {mapped}_old")
    control_database.superuser_raw(f"ALTER ROLE {other} RENAME TO {mapped}")
    control_database.roles.extend([f"{mapped}_old", mapped])
    row = pending_row(control_database)

    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(other_url.replace(f"{other}@", f"{mapped}@", 1), decide_sql(row, "user-17"))
    with pytest.raises(psycopg.Error, match=MISMATCH):
        runner_as(mapped_url.replace(f"{mapped}@", f"{mapped}_old@", 1), decide_sql(row, "user-17"))
    assert status_of(control_database, row) == "pending"
