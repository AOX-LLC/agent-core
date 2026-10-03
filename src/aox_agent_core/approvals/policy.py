"""Who may resolve an approval request."""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
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
    - the action is listed in `roles_by_action`, and the request's stored
      required_role is the role listed for it;
    - the principal holds that role;
    - the principal is not the one who requested it (no self-approval);
    - the request is still pending and has not expired at `now`.

    Checks run in that order and the first failure is the reason returned.

    The requester writes required_role when it submits, so the role an action
    needs is decided here, on the approver side, where the requester cannot
    reach: a request for an action not in `roles_by_action` is refused as
    UNKNOWN_ACTION, and one whose stored required_role differs from the listed
    role as ROLE_MISMATCH. With no map, every action is unknown and every
    request is refused.

    `trust_requester_role=True` opts out and takes the requester's
    required_role as given. It is meant for local development, where requester
    and approver are the same trusted code; never use it where the requester
    may be compromised. It cannot be combined with `roles_by_action`.
    """

    # Class defaults, so a subclass whose __init__ skips super() refuses every
    # request rather than trusting the requester.
    _roles_by_action: Mapping[str, str] = MappingProxyType({})
    _trust_requester_role: bool = False

    def __init__(
        self,
        *,
        roles_by_action: Mapping[str, str] | None = None,
        trust_requester_role: bool = False,
    ) -> None:
        if trust_requester_role and roles_by_action is not None:
            raise ValueError("pass roles_by_action or trust_requester_role=True, not both")
        self._roles_by_action = MappingProxyType(dict(roles_by_action or {}))
        self._trust_requester_role = trust_requester_role

    def evaluate(
        self, principal: Principal, request: ApprovalRequest, *, now: datetime
    ) -> ResolveVerdict:
        if principal.kind is not PrincipalKind.HUMAN:
            return _deny(DenialReason.NOT_HUMAN)
        if not self._trust_requester_role:
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
