"""Human approval of actions before they run."""

from aox_agent_core.approvals.policy import ApproverPolicy, RoleApproverPolicy
from aox_agent_core.approvals.queue import ApprovalQueue
from aox_agent_core.approvals.types import (
    TTL_SECONDS_MAX,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    DenialReason,
    Principal,
    PrincipalKind,
    ResolveVerdict,
)

__all__ = [
    "TTL_SECONDS_MAX",
    "ApprovalQueue",
    "ApprovalRequest",
    "ApprovalStatus",
    "ApproverPolicy",
    "Decision",
    "DenialReason",
    "Principal",
    "PrincipalKind",
    "ResolveVerdict",
    "RoleApproverPolicy",
]
