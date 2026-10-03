"""Who may resolve an approval request."""

from collections.abc import Mapping
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
    - with `roles_by_action`, the action is listed and the request names the
      role listed for it;
    - the principal holds the request's required_role;
    - the principal is not the one who requested it (no self-approval);
    - the request is still pending and has not expired at `now`.

    Checks run in that order and the first failure is the reason returned.

    The requester chooses required_role when it submits, so on its own that
    role only says who the requester asked for. Pass `roles_by_action` on the
    approver side, where the requester cannot change it, to decide which role
    each action needs: a request for an unlisted action is refused
    (UNKNOWN_ACTION), and so is one naming another role (ROLE_MISMATCH).
    """

    # A class default, so a subclass whose __init__ skips super() still works.
    _roles_by_action: dict[str, str] | None = None

    def __init__(self, *, roles_by_action: Mapping[str, str] | None = None) -> None:
        self._roles_by_action = dict(roles_by_action) if roles_by_action is not None else None

    def evaluate(
        self, principal: Principal, request: ApprovalRequest, *, now: datetime
    ) -> ResolveVerdict:
        if principal.kind is not PrincipalKind.HUMAN:
            return _deny(DenialReason.NOT_HUMAN)
        if self._roles_by_action is not None:
            listed = self._roles_by_action.get(request.action)
            if listed is None:
                return _deny(DenialReason.UNKNOWN_ACTION)
            if listed != request.required_role:
                return _deny(DenialReason.ROLE_MISMATCH)
        if request.required_role not in principal.roles:
            return _deny(DenialReason.MISSING_ROLE)
        if principal.id == request.requested_by:
            return _deny(DenialReason.SELF_APPROVAL)
        if request.status is ApprovalStatus.EXPIRED:
            return _deny(DenialReason.EXPIRED)
        if request.status is not ApprovalStatus.PENDING:
            return _deny(DenialReason.NOT_PENDING)
        if request.is_expired(now):
            return _deny(DenialReason.EXPIRED)
        return ResolveVerdict(allowed=True)


def _deny(reason: DenialReason) -> ResolveVerdict:
    return ResolveVerdict(allowed=False, reason=reason)
