"""Approvals on SQLite and Postgres: who may resolve, compare-and-set, single use, audited."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    DenialReason,
    Principal,
    PrincipalKind,
    ResolveVerdict,
    RoleApproverPolicy,
)
from aox_agent_core.approvals import sql as approvals_sql
from aox_agent_core.approvals.sql import SQLApprovalQueue
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    NotAuthorizedToResolveError,
)
from aox_agent_core.storage import open_database
from databases import ControlDatabase, SplitQueue, split_queue, sqlite_database

REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
OTHER_APPROVER = Principal(
    id="user-23", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"})
)
AGENT_WITH_ROLE = Principal(id="agent-reviewer", kind=PrincipalKind.AGENT, roles=APPROVER.roles)
HUMAN_REQUESTER = Principal(id="agent-intake", kind=PrincipalKind.HUMAN, roles=APPROVER.roles)
PAYLOAD = {"contact_id": "c-1001", "phone": "+1-555-0100"}
# Near the database's clock: the Postgres guard judges expiry and start times by it.
NOW = datetime.now(UTC).replace(microsecond=0)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def queue_for(database: ControlDatabase, clock: Clock | None = None) -> SplitQueue:
    return split_queue(database, clock=clock or Clock())


async def submitted(
    queue: SQLApprovalQueue | SplitQueue, requester: Principal = REQUESTER
) -> ApprovalRequest:
    return await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact's phone number",
        payload=PAYLOAD,
        requested_by=requester,
        required_role="ops.approver",
        ttl_seconds=3_600,
    )


async def audit_actions(database: ControlDatabase) -> list[tuple[str, str, Any]]:
    log = SQLAuditLog(database.database)
    return [
        (record.action, record.actor_id, record.payload.get("reason"))
        async for record in log.iter_records()
    ]


async def test_approve_then_consume_once(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)

    approved = await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    consumed = await queue.consume(
        request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
    )

    assert (approved.status, approved.resolved_by) == (ApprovalStatus.APPROVED, "user-17")
    assert consumed.status is ApprovalStatus.CONSUMED
    assert await queue.get(request.id) == consumed
    with pytest.raises(ApprovalAlreadyResolvedError):
        await queue.consume(
            request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
        )
    assert await audit_actions(control_database) == [
        ("approval.requested", "agent-intake", None),
        ("approval.resolved", "user-17", None),
        ("approval.consumed", "agent-intake", None),
        ("approval.consume_denied", "agent-intake", "not_open"),
    ]
    assert (await SQLAuditLog(control_database.database).verify()).seq == 4


@pytest.mark.parametrize(
    ("principal", "reason"),
    [
        (
            Principal(id="agent-reviewer", kind=PrincipalKind.AGENT, roles=APPROVER.roles),
            "not_human",
        ),
        (Principal(id="user-40", kind=PrincipalKind.HUMAN), "missing_role"),
    ],
)
async def test_unauthorized_resolvers_are_refused_and_audited(
    control_database: ControlDatabase, principal: Principal, reason: str
) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)

    with pytest.raises(NotAuthorizedToResolveError, match=reason):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=principal)

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING
    assert (await audit_actions(control_database))[-1] == (
        "approval.resolve_denied",
        principal.id,
        reason,
    )


async def test_self_approval_is_refused(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue, requester=APPROVER)

    with pytest.raises(NotAuthorizedToResolveError, match="self_approval"):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING
    assert (await audit_actions(control_database))[-1][2] == "self_approval"


async def test_expired_request_cannot_be_resolved(control_database: ControlDatabase) -> None:
    clock = Clock()
    queue = queue_for(control_database, clock)
    request = await submitted(queue)
    clock.now = NOW + timedelta(hours=1)

    with pytest.raises(ApprovalExpiredError):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)


async def test_resolution_is_once_only(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)
    await queue.resolve(request.id, decision=Decision.REJECT, principal=APPROVER, reason="no")

    with pytest.raises(ApprovalAlreadyResolvedError):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=OTHER_APPROVER)
    with pytest.raises(ApprovalNotGrantedError):
        await queue.consume(
            request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
        )


async def test_concurrent_resolutions_have_one_winner(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)

    outcomes = await asyncio.gather(
        queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER),
        queue.resolve(request.id, decision=Decision.REJECT, principal=OTHER_APPROVER),
        return_exceptions=True,
    )

    winners = [outcome for outcome in outcomes if isinstance(outcome, ApprovalRequest)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(winners) == 1
    assert [type(loser) for loser in losers] == [ApprovalAlreadyResolvedError]


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("crm.update_contact", {**PAYLOAD, "phone": "+1-555-0199"}),
        ("crm.delete_contact", PAYLOAD),
    ],
    ids=["changed-payload", "different-action"],
)
async def test_consume_is_bound_to_action_and_payload(
    control_database: ControlDatabase, action: str, payload: dict[str, str]
) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    with pytest.raises(ApprovalPayloadMismatchError):
        await queue.consume(request.id, action=action, payload=payload, principal=REQUESTER)

    assert (await queue.get(request.id)).status is ApprovalStatus.APPROVED


async def test_pending_request_cannot_be_consumed(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)

    with pytest.raises(ApprovalNotGrantedError):
        await queue.consume(
            request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
        )


async def test_approval_expires_before_use(control_database: ControlDatabase) -> None:
    clock = Clock()
    queue = queue_for(control_database, clock)
    request = await submitted(queue)
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    clock.now = NOW + timedelta(hours=2)

    with pytest.raises(ApprovalExpiredError):
        await queue.consume(
            request.id, action="crm.update_contact", payload=PAYLOAD, principal=REQUESTER
        )


async def test_unknown_request_is_reported_and_audited(control_database: ControlDatabase) -> None:
    queue = queue_for(control_database)

    with pytest.raises(ApprovalNotFoundError):
        await queue.resolve(uuid4(), decision=Decision.APPROVE, principal=APPROVER)
    assert (await audit_actions(control_database))[-1][2] == "not_found"


async def test_list_pending_shows_only_what_the_principal_may_resolve(
    control_database: ControlDatabase,
) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)

    assert [pending.id for pending in await queue.list_pending(APPROVER)] == [request.id]
    assert await queue.list_pending(REQUESTER) == []


async def test_failed_audit_write_rolls_back_the_resolution(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = queue_for(control_database)
    request = await submitted(queue)

    def fail(*_args: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(SQLAuditLog, "append_in", fail)
    with pytest.raises(RuntimeError, match="audit write failed"):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    monkeypatch.undo()

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING


async def test_audit_log_on_another_database_still_records(tmp_path: Path) -> None:
    approvals_db = sqlite_database(tmp_path / "approvals")
    audit_db = sqlite_database(tmp_path / "audit")
    queue = SQLApprovalQueue(approvals_db.database, audit_log=SQLAuditLog(audit_db.database))

    await submitted(queue)

    assert [action for action, _, _ in await audit_actions(audit_db)] == ["approval.requested"]


@pytest.mark.parametrize(
    ("principal", "status", "expired", "reason"),
    [
        (Principal(id="svc-batch", kind=PrincipalKind.SERVICE), "pending", False, "not_human"),
        (APPROVER.model_copy(update={"roles": frozenset()}), "pending", False, "missing_role"),
        (HUMAN_REQUESTER, "pending", False, "self_approval"),
        (APPROVER, "approved", False, "not_pending"),
        (APPROVER, "pending", True, "expired"),
        (APPROVER, "pending", False, None),
    ],
)
def test_role_policy_checks_in_order(
    principal: Principal, status: str, expired: bool, reason: str | None
) -> None:
    resolved: dict[str, Any] = (
        {"decision": Decision.APPROVE, "resolved_by": "user-99", "resolved_at": NOW}
        if status == "approved"
        else {}
    )
    request = ApprovalRequest.model_validate(
        {
            "id": UUID("00000000-0000-4000-8000-000000000001"),
            "action": "crm.update_contact",
            "summary": "s",
            "payload_sha256": "a" * 64,
            "requested_by": "agent-intake",
            "required_role": "ops.approver",
            "created_at": NOW,
            "expires_at": NOW + timedelta(hours=1),
            "status": ApprovalStatus(status),
            **resolved,
        }
    )
    now = NOW + timedelta(hours=2) if expired else NOW

    verdict = RoleApproverPolicy().evaluate(principal, request, now=now)

    assert verdict.reason == (DenialReason(reason) if reason else None)
    assert verdict.allowed is (reason is None)


async def test_consume_names_the_principal_who_acted(control_database: ControlDatabase) -> None:
    executor = Principal(id="svc-crm-writer", kind=PrincipalKind.SERVICE)
    queue = queue_for(control_database)
    request = await submitted(queue)
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    with pytest.raises(ApprovalPayloadMismatchError):
        await queue.consume(request.id, action="crm.delete_contact", payload={}, principal=executor)
    await queue.consume(
        request.id, action="crm.update_contact", payload=PAYLOAD, principal=executor
    )

    assert [(action, actor) for action, actor, _ in await audit_actions(control_database)][-2:] == [
        ("approval.consume_denied", "svc-crm-writer"),
        ("approval.consumed", "svc-crm-writer"),
    ]


async def test_separate_objects_for_one_database_share_the_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
    queue = SQLApprovalQueue(open_database(url), audit_log=SQLAuditLog(open_database(url)))
    request = await submitted(queue)

    def fail(*_args: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(SQLAuditLog, "append_in", fail)
    with pytest.raises(RuntimeError, match="audit write failed"):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    monkeypatch.undo()

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING


async def test_list_pending_filters_expired_own_and_other_role_requests(
    control_database: ControlDatabase,
) -> None:
    clock = Clock()
    queue = queue_for(control_database, clock)
    clock.now = NOW - timedelta(hours=2)
    stale = await submitted(queue)
    clock.now = NOW
    fresh = await submitted(queue)
    await submitted(queue, requester=APPROVER)
    await queue.submit(
        action="crm.update_contact",
        summary="Needs another role",
        payload=PAYLOAD,
        requested_by=REQUESTER,
        required_role="finance.approver",
        ttl_seconds=3_600,
    )

    listed = [request.id for request in await queue.list_pending(APPROVER)]

    assert listed == [fresh.id]
    assert stale.id not in listed
    assert len(await queue.list_pending(APPROVER, limit=1)) == 1


async def test_policy_denial_without_a_reason_is_still_refused(
    control_database: ControlDatabase,
) -> None:
    class DenyWithoutReason:
        def evaluate(
            self, principal: Principal, request: ApprovalRequest, *, now: datetime
        ) -> ResolveVerdict:
            return ResolveVerdict.model_construct(allowed=False, reason=None)

    queue = split_queue(control_database, policy=DenyWithoutReason(), clock=Clock())
    request = await submitted(queue)

    with pytest.raises(NotAuthorizedToResolveError, match="denied"):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING


async def test_naive_clock_is_refused(control_database: ControlDatabase) -> None:
    queue = SQLApprovalQueue(
        control_database.database,
        audit_log=SQLAuditLog(control_database.database),
        clock=lambda: datetime(2026, 10, 2, 12, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await submitted(queue)


async def test_list_pending_follows_a_custom_policy(control_database: ControlDatabase) -> None:
    class AdminsApproveAnything:
        def evaluate(
            self, principal: Principal, request: ApprovalRequest, *, now: datetime
        ) -> ResolveVerdict:
            if "admin" in principal.roles:
                return ResolveVerdict(allowed=True)
            return ResolveVerdict(allowed=False, reason=DenialReason.MISSING_ROLE)

    admin = Principal(id="user-1", kind=PrincipalKind.HUMAN, roles=frozenset({"admin"}))
    queue = split_queue(control_database, policy=AdminsApproveAnything(), clock=Clock())
    request = await submitted(queue)

    assert [pending.id for pending in await queue.list_pending(admin)] == [request.id]


async def test_list_pending_pages_past_requests_a_strict_policy_rejects(
    control_database: ControlDatabase,
) -> None:
    class OnlyTheNewest(RoleApproverPolicy):
        newest: UUID | None = None

        def evaluate(
            self, principal: Principal, request: ApprovalRequest, *, now: datetime
        ) -> ResolveVerdict:
            if request.id != self.newest:
                return ResolveVerdict(allowed=False, reason=DenialReason.MISSING_ROLE)
            return super().evaluate(principal, request, now=now)

    policy = OnlyTheNewest()
    clock = Clock()
    queue = split_queue(control_database, policy=policy, clock=clock)
    for minute in range(5):
        clock.now = NOW + timedelta(minutes=minute)
        newest = await submitted(queue)
    policy.newest = newest.id

    assert [pending.id for pending in await queue.list_pending(APPROVER, limit=2)] == [newest.id]


def test_postgres_urls_differing_in_user_or_options_are_different_databases() -> None:
    base = "postgresql://agent_core_app@127.0.0.1:4202/audit"
    same = open_database(base)

    assert open_database(base).same_database(same)
    assert not open_database(base.replace("agent_core_app", "other")).same_database(same)
    assert not open_database(f"{base}?options=-c%20search_path%3Dother").same_database(same)
    assert open_database(base.replace("app@", "app:secret@")).same_database(same)
    assert open_database("postgresql://agent_core_app@LocalHost:4202/audit").same_database(
        open_database("postgresql://agent_core_app@localhost:4202/audit")
    )
    default_port = "postgresql://agent_core_app@db.example/audit"
    assert open_database(default_port).same_database(
        open_database("postgresql://agent_core_app@db.example:5432/audit")
    )


async def test_custom_policy_listing_pages_through_tied_timestamps(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals_sql, "PENDING_PAGE_SIZE", 3)

    class EveryThird(RoleApproverPolicy):
        def __init__(self, wanted: set[UUID]) -> None:
            self.wanted = wanted

        def evaluate(
            self, principal: Principal, request: ApprovalRequest, *, now: datetime
        ) -> ResolveVerdict:
            if request.id not in self.wanted:
                return ResolveVerdict(allowed=False, reason=DenialReason.MISSING_ROLE)
            return super().evaluate(principal, request, now=now)

    policy = EveryThird(set())
    queue = split_queue(control_database, policy=policy, clock=Clock())  # one created_at
    submitted_ids = [(await submitted(queue)).id for _ in range(10)]
    in_listing_order = sorted(submitted_ids, key=str)
    policy.wanted = set(in_listing_order[::3])

    listed = [request.id for request in await queue.list_pending(APPROVER, limit=10)]

    assert listed == in_listing_order[::3]


def test_socket_paths_keep_their_case_and_pgport_sets_the_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def url(host: str) -> str:
        return f"postgresql://agent_core_app@/audit?host={host}"

    assert not open_database(url("/run/pg/A")).same_database(open_database(url("/run/pg/a")))

    monkeypatch.setenv("PGPORT", "5433")
    without_port = open_database("postgresql://agent_core_app@db.example/audit")
    assert without_port.same_database(
        open_database("postgresql://agent_core_app@db.example:5433/audit")
    )
    assert not without_port.same_database(
        open_database("postgresql://agent_core_app@db.example:5432/audit")
    )


async def test_a_full_listing_reads_no_extra_page(
    control_database: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = queue_for(control_database)
    for _ in range(2):
        await submitted(queue)
    transactions = 0
    # Listing is the approver's: it runs on the approver's connection.
    run = control_database.approver_database.run

    async def counting_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal transactions
        transactions += 1
        return await run(*args, **kwargs)

    monkeypatch.setattr(control_database.approver_database, "run", counting_run)

    assert len(await queue.list_pending(APPROVER, limit=2)) == 2
    assert transactions == 1


async def test_list_pending_pages_with_an_after_cursor(control_database: ControlDatabase) -> None:
    clock = Clock()
    queue = queue_for(control_database, clock)
    created = []
    for minute in range(5):
        clock.now = NOW + timedelta(minutes=minute)
        created.append((await submitted(queue)).id)

    first = await queue.list_pending(APPROVER, limit=2)
    second = await queue.list_pending(APPROVER, limit=2, after=first[-1].id)
    third = await queue.list_pending(APPROVER, limit=2, after=second[-1].id)

    pages = [[request.id for request in page] for page in (first, second, third)]
    assert pages == [created[0:2], created[2:4], created[4:5]]
    with pytest.raises(ApprovalNotFoundError):
        await queue.list_pending(APPROVER, after=uuid4())
