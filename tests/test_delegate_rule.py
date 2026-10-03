"""A delegate may use an approval but never grant it."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    DenialReason,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
)
from aox_agent_core.approvals.sql import approval_payload_hash
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.errors import NotAuthorizedToResolveError
from databases import TEST_ACTION_ROLES, ControlDatabase, split_queue
from test_approval_payload import ACTION, APPROVALS, raw_request, submit
from test_approvals import APPROVER, OTHER_APPROVER, PAYLOAD, REQUESTER, audit_actions


def test_the_policy_refuses_a_named_delegate() -> None:
    now = datetime.now(UTC)
    request = ApprovalRequest(
        id=uuid4(),
        action=ACTION,
        summary="s",
        payload_sha256=approval_payload_hash(ACTION, PAYLOAD),
        requested_by=REQUESTER.id,
        required_role="ops.approver",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        delegates=frozenset({APPROVER.id}),
    )
    policy = RoleApproverPolicy(roles_by_action=TEST_ACTION_ROLES)

    refused = policy.evaluate(APPROVER, request, now=now)
    allowed = policy.evaluate(OTHER_APPROVER, request, now=now)

    assert (refused.allowed, refused.reason) == (False, DenialReason.DELEGATE_APPROVAL)
    assert allowed.allowed


async def test_a_delegate_cannot_approve_but_another_approver_can(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    request = await submit(queue, delegates={APPROVER.id})

    with pytest.raises(NotAuthorizedToResolveError, match="delegate_approval"):
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING
    assert (await audit_actions(control_database))[-1][2] == "delegate_approval"
    # A delegate is not offered the request to decide, either.
    assert await queue.list_pending(APPROVER) == []
    assert [r.id for r in await queue.list_pending(OTHER_APPROVER)] == [request.id]

    resolved = await queue.resolve(request.id, decision=Decision.APPROVE, principal=OTHER_APPROVER)
    assert resolved.status is ApprovalStatus.APPROVED
    # The delegate still uses it: that is what a delegate is for.
    consumed = await queue.consume(
        request.id,
        action=ACTION,
        payload={"contact_id": "c-1001", "phone": "+1-555-0100"},
        principal=Principal(id=APPROVER.id, kind=PrincipalKind.AGENT),
    )
    assert consumed.status is ApprovalStatus.CONSUMED


async def test_the_database_refuses_a_delegate_as_the_approver(
    control_database: ControlDatabase,
) -> None:
    if control_database.requester_raw is None or control_database.approver_raw is None:
        pytest.skip("the guard trigger exists only on Postgres")
    row = uuid4()
    control_database.requester_raw(raw_request(id=f"'{row}'", delegates="""'["user-17"]'"""))
    decided = canonical_timestamp(datetime.now(UTC))
    decide = (
        f"UPDATE {APPROVALS} SET status = 'approved', decision = 'approve', "
        f"resolved_by = '%s', resolved_at = '{decided}' WHERE id = '{row}'"
    )

    with pytest.raises(psycopg.Error):
        control_database.approver_raw(decide % "user-17")
    control_database.approver_raw(decide % "user-23")
    assert control_database.raw(f"SELECT resolved_by FROM {APPROVALS} WHERE id = '{row}'") == [
        ("user-23",)
    ]
