"""Who may resolve an approval request."""

from datetime import datetime
from typing import Protocol

from aox_agent_core.approvals.types import (
    ApprovalRequest,
    ApprovalStatus,
    DenialReason,
    Principal,
    PrincipalKind,
    ResolveVerdict,
)


class ApproverPolicy(Protocol):
    """Decides whether a principal may resolve a request.

    ApprovalQueue.resolve() calls the policy itself, so a caller cannot skip it.
    """

    def evaluate(
        self, principal: Principal, request: ApprovalRequest, *, now: datetime
    ) -> ResolveVerdict: ...


class RoleApproverPolicy:
    """The default policy. A principal may resolve a request only if all hold:

    - the principal is a human (agents and services never approve);
    - the principal holds the request's required_role;
    - the principal is not the one who requested it (no self-approval);
    - the request is still pending and has not expired at `now`.

    Checks run in that order and the first failure is the reason returned.
    """

    def evaluate(
        self, principal: Principal, request: ApprovalRequest, *, now: datetime
    ) -> ResolveVerdict:
        if principal.kind is not PrincipalKind.HUMAN:
            return _deny(DenialReason.NOT_HUMAN)
        if request.required_role not in principal.roles:
            return _deny(DenialReason.MISSING_ROLE)
        if principal.id == request.requested_by:
            return _deny(DenialReason.SELF_APPROVAL)
        if request.status is not ApprovalStatus.PENDING:
            return _deny(DenialReason.NOT_PENDING)
        if request.is_expired(now):
            return _deny(DenialReason.EXPIRED)
        return ResolveVerdict(allowed=True)


def _deny(reason: DenialReason) -> ResolveVerdict:
    return ResolveVerdict(allowed=False, reason=reason)
