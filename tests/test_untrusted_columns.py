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
from uuid import uuid4

import psycopg
import pytest

from aox_agent_core.approvals import ApprovalStatus, Decision
from aox_agent_core.audit import AuditEvent
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import ApprovalError, AuditIntegrityError
from databases import ControlDatabase, split_queue
from test_approval_payload import ACTION, raw_request
from test_approvals import APPROVER, REQUESTER
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
