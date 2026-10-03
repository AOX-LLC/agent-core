"""Opt-in storage of the payload an approver sees, bound to payload_sha256 on every read."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from aox_agent_core.approvals import ApprovalRequest, ApprovalStatus, Decision
from aox_agent_core.approvals.sql import approval_payload_hash
from aox_agent_core.audit import sql as audit_sql
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import (
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalPayloadRejectedError,
)
from databases import ControlDatabase, SplitQueue, split_queue
from test_approvals import APPROVER, PAYLOAD, REQUESTER

ACTION = "crm.update_contact"
APPROVALS = "agent_core_approvals"


async def submit(queue: SplitQueue, **options: Any) -> ApprovalRequest:
    return await queue.submit(
        action=ACTION,
        summary="Update the sample contact's phone number",
        payload=options.pop("payload", PAYLOAD),
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
        **options,
    )


def tamper(database: ControlDatabase, request: ApprovalRequest, payload: dict[str, Any]) -> None:
    """What an owner can do: switch the guard off, rewrite the stored payload, switch it on."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    update = f"UPDATE {APPROVALS} SET payload_json = '{text}' WHERE id = '{request.id}'"
    if database.backend == "postgres":
        guard = "agent_core_approvals_guard"
        database.raw(f"ALTER TABLE {APPROVALS} DISABLE TRIGGER {guard}")
        database.raw(update)
        database.raw(f"ALTER TABLE {APPROVALS} ENABLE TRIGGER {guard}")
    else:
        database.raw(update)


async def actions(database: ControlDatabase) -> list[tuple[str, Any]]:
    log = SQLAuditLog(database.database)
    return [(r.action, r.payload.get("reason")) async for r in log.iter_records()]


async def test_a_stored_payload_is_what_the_approver_sees(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)

    request = await submit(queue, include_payload=True)

    assert request.payload == PAYLOAD
    assert approval_payload_hash(ACTION, PAYLOAD) == request.payload_sha256
    assert (await queue.get(request.id)).payload == PAYLOAD
    assert [r.payload for r in await queue.list_pending(APPROVER)] == [PAYLOAD]
    decided = await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    assert decided.payload == PAYLOAD
    consumed = await queue.consume(request.id, action=ACTION, payload=PAYLOAD, principal=REQUESTER)
    assert consumed.status is ApprovalStatus.CONSUMED


async def test_nothing_is_stored_unless_asked(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)

    request = await submit(queue)

    assert request.payload is None
    assert (await queue.get(request.id)).payload is None
    assert control_database.raw(f"SELECT payload_json FROM {APPROVALS}") == [(None,)]


async def test_the_payload_never_reaches_the_audit_log(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)
    request = await submit(queue, include_payload=True)
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    rows = control_database.raw(f"SELECT payload FROM {audit_sql.AUDIT_TABLE}")

    assert rows
    assert not any("c-1001" in row[0] or "555" in row[0] for row in rows)


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"blob": "x" * 9_000}, "limit"),
        ({"auth_token": "abc"}, "forbidden keys"),
        ({"amount": 12.5}, "integers"),
        ({"note": "key sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"}, "anthropic"),
    ],
)
async def test_a_payload_that_may_not_be_stored_is_refused_and_nothing_is_written(
    control_database: ControlDatabase, payload: dict[str, Any], why: str
) -> None:
    queue = split_queue(control_database)

    with pytest.raises(ApprovalPayloadRejectedError, match=why):
        await submit(queue, payload=payload, include_payload=True)

    assert await actions(control_database) == []


async def test_the_same_payload_may_still_be_approved_without_being_stored(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)

    request = await submit(queue, payload={"blob": "x" * 9_000})

    assert request.payload is None


async def test_a_tampered_payload_is_never_shown_and_cannot_be_approved(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue, include_payload=True)
    tamper(control_database, request, {"contact_id": "c-9999", "phone": "+1-555-0199"})

    with pytest.raises(ApprovalIntegrityError):
        await queue.get(request.id)
    assert await queue.list_pending(APPROVER) == []
    with pytest.raises(ApprovalIntegrityError):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    status = control_database.raw(f"SELECT status FROM {APPROVALS} WHERE id = '{request.id}'")
    assert status == [("pending",)]
    assert ("approval.resolve_denied", "payload_integrity") in await actions(control_database)


async def test_the_requester_can_still_withdraw_a_request_whose_payload_is_tampered(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue, include_payload=True)
    tamper(control_database, request, {"contact_id": "c-9999"})

    cancelled = await queue.cancel(request.id, principal=REQUESTER)

    assert cancelled.status is ApprovalStatus.CANCELLED


async def test_one_tampered_request_does_not_hide_the_others_from_the_approver(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    bad = await submit(queue, include_payload=True)
    good = await submit(queue, include_payload=True)
    tamper(control_database, bad, {"contact_id": "c-9999"})

    assert [r.id for r in await queue.list_pending(APPROVER)] == [good.id]


# The database's own rules (Postgres)


def raw_request(**columns: str) -> str:
    now = datetime.now(UTC)
    values = {
        "id": f"'{uuid4()}'",
        "action": f"'{ACTION}'",
        "summary": "'forged'",
        "payload_sha256": f"'{'c' * 64}'",
        "requested_by": "'agent-intake'",
        "required_role": "'ops.approver'",
        "created_at": f"'{canonical_timestamp(now)}'",
        "expires_at": f"'{canonical_timestamp(now + timedelta(hours=1))}'",
        "status": "'pending'",
        "delegates": "'[]'",
        **columns,
    }
    return f"INSERT INTO {APPROVALS} ({', '.join(values)}) VALUES ({', '.join(values.values())})"


async def test_a_request_the_requester_inserted_with_a_mismatched_payload_is_not_shown(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    queue = split_queue(control_database)
    await submit(queue)  # creates the table's first row, so the library has looked at it
    control_database.requester_raw(raw_request(payload_json='\'{"contact_id":"c-9999"}\''))

    pending = await queue.list_pending(APPROVER)

    assert [r.payload for r in pending] == [None]


@pytest.mark.parametrize(
    "payload_json",
    ["'[1, 2]'", "'not json'", f"'{json.dumps({'x': 'y' * 9_000})}'"],
)
async def test_the_database_refuses_a_stored_payload_of_the_wrong_shape(
    control_database: ControlDatabase, payload_json: str
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    await submit(split_queue(control_database))

    with pytest.raises(psycopg.Error):
        control_database.requester_raw(raw_request(payload_json=payload_json))


async def test_neither_role_can_change_a_stored_payload(control_database: ControlDatabase) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("roles exist only on Postgres")
    request = await submit(split_queue(control_database), include_payload=True)
    update = f"UPDATE {APPROVALS} SET payload_json = '{{}}' WHERE id = '{request.id}'"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        control_database.requester_raw(update)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        control_database.approver_raw(update)
    with pytest.raises(psycopg.Error, match="never change"):
        control_database.raw(update)  # even the owner, while the guard is on


async def test_an_unknown_request_is_still_not_found(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)
    await submit(queue, include_payload=True)

    with pytest.raises(ApprovalNotFoundError):
        await queue.get(uuid4())


# What the requester can write with plain SQL (Postgres) must not hurt the approver


def raw_with_payload(payload_text: str, *, sha: str | None = None, **columns: str) -> str:
    return raw_request(
        payload_json=f"'{payload_text}'",
        payload_sha256=f"'{sha}'" if sha else f"'{'c' * 64}'",
        **columns,
    )


HOSTILE_PAYLOADS = {
    "infinite-number": '{"a": 1e400}',
    "huge-integer": '{"n": ' + "9" * 5_000 + "}",
    "deep-nesting": '{"a":' + "[" * 3_500 + "]" * 3_500 + "}",
}


@pytest.mark.parametrize("payload_text", HOSTILE_PAYLOADS.values(), ids=HOSTILE_PAYLOADS.keys())
async def test_a_hostile_stored_payload_hides_only_its_own_request(
    control_database: ControlDatabase, payload_text: str
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    queue = split_queue(control_database)
    honest = await submit(queue, include_payload=True)
    bad_id = uuid4()
    control_database.requester_raw(raw_with_payload(payload_text, id=f"'{bad_id}'"))

    assert [r.id for r in await queue.list_pending(APPROVER)] == [honest.id]
    with pytest.raises(ApprovalIntegrityError):
        await queue.get(bad_id)
    with pytest.raises(ApprovalIntegrityError):
        await queue.resolve(bad_id, decision=Decision.APPROVE, principal=APPROVER)
    assert ("approval.resolve_denied", "payload_integrity") in await actions(control_database)
    assert (await queue.resolve(honest.id, decision=Decision.APPROVE, principal=APPROVER)).payload


async def test_a_hostile_row_does_not_stop_the_expiry_sweep(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    queue = split_queue(control_database)
    now = datetime.now(UTC)
    due = {
        "created_at": f"'{canonical_timestamp(now - timedelta(hours=2))}'",
        "expires_at": f"'{canonical_timestamp(now - timedelta(hours=1))}'",
    }
    await submit(queue)  # makes sure the table is known to the library
    control_database.requester_raw(raw_with_payload(HOSTILE_PAYLOADS["deep-nesting"], **due))
    control_database.requester_raw(raw_with_payload(HOSTILE_PAYLOADS["infinite-number"], **due))

    assert await queue.expire_due(principal=REQUESTER) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": 12.5},
        {"amount": 9_007_199_254_740_993},
        {"api_key": "abc"},
        {"note": "key sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"},
    ],
    ids=["float", "unsafe-integer", "secret-key", "secret-text"],
)
async def test_a_stored_payload_that_a_submit_would_refuse_is_not_shown(
    control_database: ControlDatabase, payload: dict[str, Any]
) -> None:
    if control_database.requester_raw is None:
        pytest.skip("a plain-SQL requester role exists only on Postgres")
    queue = split_queue(control_database)
    await submit(queue)
    bad_id = uuid4()
    # The hash matches: the requester computed it itself, so only the rules can catch it.
    sha = approval_payload_hash(ACTION, payload)
    control_database.requester_raw(raw_with_payload(json.dumps(payload), sha=sha, id=f"'{bad_id}'"))

    assert [r.id for r in await queue.list_pending(APPROVER)] != [bad_id]
    assert bad_id not in [r.id for r in await queue.list_pending(APPROVER)]
    with pytest.raises(ApprovalIntegrityError):
        await queue.get(bad_id)


async def test_consume_and_cancel_return_no_payload(control_database: ControlDatabase) -> None:
    queue = split_queue(control_database)
    used = await submit(queue, include_payload=True)
    withdrawn = await submit(queue, include_payload=True)
    await queue.resolve(used.id, decision=Decision.APPROVE, principal=APPROVER)

    consumed = await queue.consume(used.id, action=ACTION, payload=PAYLOAD, principal=REQUESTER)
    cancelled = await queue.cancel(withdrawn.id, principal=REQUESTER)

    assert consumed.payload is None
    assert cancelled.payload is None


async def test_a_payload_with_a_nul_is_refused_not_crashed_on(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)

    with pytest.raises(ApprovalPayloadRejectedError, match="NUL"):
        await submit(queue, payload={"note": "a\x00b"}, include_payload=True)
