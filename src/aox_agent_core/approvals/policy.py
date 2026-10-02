"""Who may resolve an approval request."""

from datetime import datetime
from typing import Protocol

from aox_agent_core.approvals.types import ApprovalRequest, Principal, ResolveVerdict


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
        raise NotImplementedError("RoleApproverPolicy.evaluate is not implemented yet.")
