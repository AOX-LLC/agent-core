"""Free text a person reads: no control, bidirectional or format characters, and NUL in payloads."""

import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError

from aox_agent_core import _postgres_schema as layout
from aox_agent_core._text import neutralized
from aox_agent_core.approvals import Decision
from aox_agent_core.audit import AuditEvent
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.audit.types import check_payload
from databases import ControlDatabase, split_queue
from test_approval_payload import ACTION, APPROVALS, raw_request, submit
from test_approvals import APPROVER, REQUESTER

# Built from code points: a literal control or bidi character in source is itself the hazard.
UNSAFE_CHARACTERS = ("\x1b", chr(0x202E), chr(0x200B), chr(0x2028), chr(0x85), "\n", chr(0xFEFF))
UNSAFE = [f"a{character}b" for character in (*UNSAFE_CHARACTERS, chr(0x2066))]
UNSAFE_IDS = [
    "escape",
    "rlo-override",
    "zero-width-space",
    "line-separator",
    "nel",
    "newline",
    "bom",
    "isolate",
]


def _in_guard_pattern() -> list[str]:
    """Every character the Postgres guard's pattern covers."""
    characters: list[str] = []
    body = layout.UNSAFE_TEXT_PATTERN[1:-1]
    index = 0
    while index < len(body):
        start = int(body[index + 2 : index + 6], 16)
        index += 6
        end = start
        if index < len(body) and body[index] == "-":
            end = int(body[index + 3 : index + 7], 16)
            index += 7
        characters += [chr(code) for code in range(start, end + 1)]
    return characters


def test_the_database_pattern_never_covers_a_character_the_library_accepts() -> None:
    covered = _in_guard_pattern()
    assert len(covered) > 100
    wrongly = [
        hex(ord(c)) for c in covered if unicodedata.category(c) not in {"Cc", "Cf", "Zl", "Zp"}
    ]
    assert wrongly == []


def test_ordinary_text_passes_and_unsafe_text_is_replaced_for_display() -> None:
    assert neutralized("Zoë 北京 — ok") == "Zoë 北京 — ok"
    assert neutralized(f"a{chr(0x202E)}b\x1b[31m") == "a\ufffdb\ufffd[31m"


@pytest.mark.parametrize("text", UNSAFE, ids=UNSAFE_IDS)
async def test_a_summary_with_such_a_character_is_refused(
    control_database: ControlDatabase, text: str
) -> None:
    queue = split_queue(control_database)
    with pytest.raises(ValidationError):
        await queue.submit(
            action=ACTION,
            summary=text,
            payload={"n": 1},
            requested_by=REQUESTER,
            required_role="ops.approver",
            ttl_seconds=60,
        )


@pytest.mark.parametrize("text", UNSAFE, ids=UNSAFE_IDS)
async def test_a_reason_or_cancel_reason_with_such_a_character_is_refused(
    control_database: ControlDatabase, text: str
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue)
    with pytest.raises(ValueError, match="control or bidirectional"):
        await queue.resolve(request.id, decision=Decision.REJECT, principal=APPROVER, reason=text)
    with pytest.raises(ValueError, match="control or bidirectional"):
        await queue.cancel(request.id, principal=REQUESTER, reason=text)
    # Nothing was decided or audited by the refused calls.
    assert (await queue.get(request.id)).status.value == "pending"


@pytest.mark.parametrize("text", UNSAFE, ids=UNSAFE_IDS)
async def test_the_database_refuses_such_a_summary_and_reason_from_plain_sql(
    control_database: ControlDatabase, text: str
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    quoted = "'" + text.replace("'", "''") + "'"
    with pytest.raises(psycopg.Error):
        control_database.requester_raw(raw_request(summary=quoted))

    honest = uuid4()
    control_database.requester_raw(raw_request(id=f"'{honest}'"))
    decided = canonical_timestamp(datetime.now(UTC))
    with pytest.raises(psycopg.Error):
        control_database.approver_raw(
            f"UPDATE {APPROVALS} SET status = 'rejected', decision = 'reject', "
            f"resolved_by = 'user-17', resolved_at = '{decided}', reason = {quoted} "
            f"WHERE id = '{honest}'"
        )


async def test_a_summary_stored_before_the_rule_is_shown_neutralized(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue)
    hostile = f"Pay {chr(0x202E)}elbaT{chr(0x202C)} \x1b[31mred"
    quoted = "'" + hostile + "'"
    update = f"UPDATE {APPROVALS} SET summary = {quoted} WHERE id = '{request.id}'"
    if control_database.backend == "postgres":
        guard = "agent_core_approvals_guard"
        control_database.raw(f"ALTER TABLE {APPROVALS} DISABLE TRIGGER {guard}")
        control_database.raw(update)
        control_database.raw(f"ALTER TABLE {APPROVALS} ENABLE TRIGGER {guard}")
    else:
        control_database.raw(update)

    read = await queue.get(request.id)
    listed = await queue.list_pending(APPROVER)
    assert read.summary == neutralized(hostile)
    assert [r.summary for r in listed] == [neutralized(hostile)]
    assert chr(0x202E) not in read.summary
    assert chr(0x202E) not in read.summary


@pytest.mark.parametrize(
    "payload",
    [{"a": "x\x00y"}, {"a\x00": 1}, {"a": ["x", {"b": "\x00"}]}],
    ids=["value", "key", "nested"],
)
async def test_nul_is_refused_in_any_audit_payload(
    control_database: ControlDatabase, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="NUL"):
        check_payload(payload)
    with pytest.raises(ValidationError):
        AuditEvent(action="model.call", actor_id="svc", payload=payload)
    log = SQLAuditLog(control_database.database)
    assert (await log.head()).seq == 0


async def test_a_cancel_reason_with_nul_is_refused_before_it_reaches_the_audit_log(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue)
    with pytest.raises(ValueError, match="control or bidirectional"):
        await queue.cancel(request.id, principal=REQUESTER, reason="a\x00b")
    log = SQLAuditLog(control_database.database)
    assert [r.action async for r in log.iter_records()] == ["approval.requested"]
