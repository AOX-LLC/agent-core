"""Run contexts in audit records and approvals, and refusal of 0.1.0a1 tables."""

import copy
import pickle
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aox_agent_core import RunContext
from aox_agent_core._validation import STORED_RECORD
from aox_agent_core.approvals import Principal, PrincipalKind
from aox_agent_core.approvals import sql as approvals_sql
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.approvals.types import Decision
from aox_agent_core.audit import (
    AUDIT_SCHEMA_VERSION,
    GENESIS_HASH,
    AuditEvent,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.audit import sql as audit_sql
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import (
    ApprovalNotGrantedError,
    AuditIntegrityError,
    AuditPayloadRejectedError,
    ConfigError,
)
from aox_agent_core.replay import PatternScrubber, SecretFinding
from aox_agent_core.storage import open_database, table_columns
from databases import ControlDatabase, SplitQueue, split_queue

RUN = RunContext(run_id="run-0001", external_ids={"workflow_id": "wf-7"})
REVIEW_RUN = RunContext(run_id="review-0009")
REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
PAYLOAD = {"contact_id": "c-1001"}


def event(context: RunContext | None = RUN) -> AuditEvent:
    return AuditEvent(
        action="model.call", actor_id="svc-intake", payload={"tier": "small"}, context=context
    )


def queue_for(database: ControlDatabase) -> SplitQueue:
    return split_queue(database)


async def submit(queue: SQLApprovalQueue | SplitQueue, context: RunContext | None = RUN) -> Any:
    return await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
        context=context,
    )


# Audit records


async def test_a_record_stores_its_run_context(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)

    appended = await log.append(event())
    await log.append(event(context=None))

    with_context, without = [record async for record in log.iter_records()]
    assert appended.schema_version == AUDIT_SCHEMA_VERSION == 2
    assert with_context.run_context == RUN
    assert without.run_context is None
    assert (await log.verify()).seq == 2


def test_the_run_context_is_part_of_the_record_hash() -> None:
    fields: dict[str, Any] = {
        "seq": 1,
        "event_id": "6f2b8a43-46bb-4f7e-9d43-27c3f2f0a6f1",
        "occurred_at": "2026-10-02T12:00:00Z",
        "action": "model.call",
        "actor_id": "svc-intake",
        "subject_id": None,
        "payload": {},
        "prev_hash": GENESIS_HASH,
    }
    hashes = {
        compute_record_hash(UnsealedAuditRecord.model_validate({**fields, "run_context": context}))
        for context in (None, RUN, REVIEW_RUN)
    }

    assert len(hashes) == 3


async def test_an_edited_run_context_fails_verification(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)
    await log.append(event())
    for trigger in _update_triggers(control_database):
        control_database.raw(trigger)

    control_database.superuser_raw(
        f"UPDATE {audit_sql.AUDIT_TABLE} SET run_context = "
        """'{"external_ids":{},"run_id":"run-9999"}' WHERE seq = 1"""
    )

    with pytest.raises(AuditIntegrityError, match="altered"):
        await log.verify()


def test_an_audit_event_refuses_a_secret_shaped_context() -> None:
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                "action": "model.call",
                "actor_id": "svc-intake",
                "context": {"run_id": "r1", "external_ids": {"api_token": "abc"}},
            }
        )


ACME_PATTERNS = {"acme_token": r"acme_[a-f0-9]{16}"}
ACME_RUN = RunContext(run_id="acme_abcdefabcdef1234")


async def test_the_logs_own_patterns_apply_to_the_context(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(
        control_database.database, scrubber=PatternScrubber(extra_patterns=ACME_PATTERNS)
    )

    with pytest.raises(AuditPayloadRejectedError, match=r"acme_token at \$\.context\.run_id"):
        await log.append(event(context=ACME_RUN))

    assert [record async for record in log.iter_records()] == []


async def test_an_approval_with_such_a_context_is_not_stored(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database, scrubber=PatternScrubber(extra_patterns=ACME_PATTERNS))

    with pytest.raises(AuditPayloadRejectedError):
        await submit(queue, context=ACME_RUN)

    assert await queue.list_pending(APPROVER) == []


async def test_stored_contexts_are_not_scanned_again_on_read(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SQLAuditLog(control_database.database)
    await log.append(event())
    queue = queue_for(control_database)
    request = await submit(queue)

    # A later release adds a pattern that this stored context happens to match.
    def finds_everything(self: PatternScrubber, value: object) -> list[SecretFinding]:
        return [SecretFinding(rule="new_rule", path="$")]

    monkeypatch.setattr(PatternScrubber, "find_secrets", finds_everything)

    assert (await log.verify()).seq == 2
    assert (await queue.get(request.id)).run_context == RUN


def test_a_run_context_is_read_only_and_hashable() -> None:
    with pytest.raises(TypeError):
        RUN.external_ids["workflow_id"] = "wf-8"  # type: ignore[index]

    assert hash(RUN) == hash(RunContext(run_id="run-0001", external_ids={"workflow_id": "wf-7"}))
    assert RUN.model_dump() == {"run_id": "run-0001", "external_ids": {"workflow_id": "wf-7"}}


# Approvals


async def test_submit_stores_the_context_and_audits_it(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)

    request = await submit(queue)

    assert request.run_context == RUN
    assert (await queue.get(request.id)).run_context == RUN
    (record,) = [r async for r in SQLAuditLog(control_database.database).iter_records()]
    assert (record.action, record.run_context) == ("approval.requested", RUN)


async def test_resolve_and_consume_audit_their_own_context_or_the_requests(
    control_database: ControlDatabase,
) -> None:
    queue = queue_for(control_database)
    request = await submit(queue)

    await queue.resolve(
        request.id, decision=Decision.APPROVE, principal=APPROVER, context=REVIEW_RUN
    )
    await queue.consume(
        request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
    )

    records = [r async for r in SQLAuditLog(control_database.database).iter_records()]
    assert [(r.action, r.run_context) for r in records] == [
        ("approval.requested", RUN),
        ("approval.resolved", REVIEW_RUN),
        ("approval.consumed", RUN),
    ]


async def test_a_denied_attempt_is_audited_with_the_context(
    control_database: ControlDatabase,
) -> None:
    queue = queue_for(control_database)
    request = await submit(queue, context=None)

    with pytest.raises(ApprovalNotGrantedError):
        await queue.consume(
            request.id,
            action="crm.update_contact",
            payload=PAYLOAD,
            principal=REQUESTER,
            context=REVIEW_RUN,
        )

    records = [r async for r in SQLAuditLog(control_database.database).iter_records()]
    assert [(r.action, r.run_context) for r in records] == [
        ("approval.requested", None),
        ("approval.consume_denied", REVIEW_RUN),
    ]


# Tables from 0.1.0a1


async def test_an_audit_table_from_0_1_0a1_is_refused(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)
    await log.append(event())
    control_database.raw(f"ALTER TABLE {audit_sql.AUDIT_TABLE} DROP COLUMN run_context")

    with pytest.raises(ConfigError, match=r"created by agent-core 0\.1\.0a1"):
        await SQLAuditLog(control_database.database).append(event())
    with pytest.raises(ConfigError, match="no run_context column"):
        await log.verify()


async def test_an_approvals_table_from_0_1_0a1_is_refused(
    control_database: ControlDatabase,
) -> None:
    queue = queue_for(control_database)
    request = await submit(queue)
    control_database.raw(f"ALTER TABLE {approvals_sql.APPROVALS_TABLE} DROP COLUMN run_context")

    with pytest.raises(ConfigError, match=r"created by agent-core 0\.1\.0a1"):
        await queue.get(request.id)
    with pytest.raises(ConfigError, match="no run_context column"):
        await submit(queue)


def _update_triggers(database: ControlDatabase) -> list[str]:
    """Statements that drop the triggers blocking UPDATE, as an attacker with owner rights would."""
    if database.backend == "sqlite":
        return [f"DROP TRIGGER {audit_sql.UPDATE_TRIGGER}"]
    return [f"DROP TRIGGER {audit_sql.UPDATE_DELETE_TRIGGER} ON {audit_sql.AUDIT_TABLE}"]


def test_table_columns_refuses_a_name_that_is_not_an_identifier(tmp_path: Path) -> None:
    database = open_database(f"sqlite:///{tmp_path / 'x.sqlite3'}")

    with pytest.raises(ValueError, match="not a plain table name"):
        database.run_sync(lambda session: table_columns(session, "x); DROP TABLE y; --"))


def test_contexts_and_what_holds_them_copy_and_pickle() -> None:
    held = event()

    assert pickle.loads(pickle.dumps(RUN)) == RUN  # noqa: S301 - bytes pickled just above
    assert copy.deepcopy(held) == held
    assert RUN.model_copy(deep=True) == RUN


def test_a_stored_context_is_checked_for_structure_only() -> None:
    stored = {"run_id": "r1", "external_ids": {"api_token": "abc"}}

    assert RunContext.model_validate(stored, context={STORED_RECORD: True}).run_id == "r1"
    with pytest.raises(ValidationError):
        RunContext.model_validate(stored)
    with pytest.raises(ValidationError):
        RunContext.model_validate({"run_id": "not an id!"}, context={STORED_RECORD: True})
