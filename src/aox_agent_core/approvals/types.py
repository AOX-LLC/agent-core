"""Approval requests, the people who resolve them, and the policy's verdict."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, StringConstraints, model_validator

from aox_agent_core._model import ActionName, FrozenModel, PrincipalId, Sha256Hex

RoleName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]

TTL_SECONDS_MAX = 7 * 24 * 60 * 60


class PrincipalKind(StrEnum):
    """What kind of actor a principal is. Only humans can resolve approvals by default."""

    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"


class Principal(FrozenModel):
    """An actor the consuming application has already authenticated.

    The library does not authenticate anyone; it authorizes. The application
    builds a Principal from its own session and passes it in.
    """

    id: PrincipalId
    kind: PrincipalKind
    roles: frozenset[RoleName] = frozenset()


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalRequest(FrozenModel):
    """A request for a human to approve one action.

    payload_sha256 binds the approval to the exact payload submitted: the action
    may run only with a payload that hashes to the same value.
    """

    id: UUID
    action: ActionName
    summary: ShortText
    payload_sha256: Sha256Hex
    requested_by: PrincipalId
    required_role: RoleName
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: Decision | None = None
    resolved_by: PrincipalId | None = None
    resolved_at: AwareDatetime | None = None
    reason: ShortText | None = None

    @model_validator(mode="after")
    def _expires_after_creation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class DenialReason(StrEnum):
    """Why a principal may not resolve a request. Recorded in the audit log."""

    NOT_HUMAN = "not_human"
    MISSING_ROLE = "missing_role"
    SELF_APPROVAL = "self_approval"
    NOT_PENDING = "not_pending"
    EXPIRED = "expired"


class ResolveVerdict(FrozenModel):
    """The policy's answer: allowed, or denied with a reason."""

    allowed: bool
    reason: DenialReason | None = None

    @model_validator(mode="after")
    def _denial_has_reason(self) -> Self:
        if self.allowed == (self.reason is not None):
            raise ValueError("a denial needs a reason and an approval must not have one")
        return self
