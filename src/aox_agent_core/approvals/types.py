"""Approval requests, the people who resolve them, and the policy's verdict."""

from collections.abc import Mapping
from datetime import datetime, timedelta
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
    """Where a request is in its life. CONSUMED means its one permitted run has happened.

    EXPIRED and CANCELLED are reserved: this release never sets them. Expiry is
    judged from expires_at whenever a request is resolved or used.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


# The decision each status implies. A request has resolved_by and resolved_at
# exactly when it has a decision.
_DECISION_FOR_STATUS: Mapping[ApprovalStatus, Decision | None] = {
    ApprovalStatus.PENDING: None,
    ApprovalStatus.APPROVED: Decision.APPROVE,
    ApprovalStatus.REJECTED: Decision.REJECT,
    ApprovalStatus.CONSUMED: Decision.APPROVE,
    ApprovalStatus.EXPIRED: None,
    ApprovalStatus.CANCELLED: None,
}


class ApprovalRequest(FrozenModel):
    """A request for a human to approve one run of one action.

    payload_sha256 is the SHA-256 of the canonical JSON (keys sorted, no
    insignificant whitespace, UTF-8) of {"action": action, "payload": payload}.
    Hashing the action with the payload means an approval for one action can
    never authorize a different action that happens to share its payload.
    An approval authorizes a single run: using it moves it to CONSUMED.
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
    consumed_at: AwareDatetime | None = None
    reason: ShortText | None = None

    @model_validator(mode="after")
    def _lifetime_is_bounded(self) -> Self:
        lifetime = self.expires_at - self.created_at
        if lifetime <= timedelta(0):
            raise ValueError("expires_at must be later than created_at")
        if lifetime > timedelta(seconds=TTL_SECONDS_MAX):
            raise ValueError(f"a request may live at most {TTL_SECONDS_MAX} seconds")
        return self

    @model_validator(mode="after")
    def _state_is_consistent(self) -> Self:
        if self.decision != _DECISION_FOR_STATUS[self.status]:
            raise ValueError(f"status {self.status.value} does not match decision {self.decision}")

        is_resolved = self.decision is not None
        if (self.resolved_by is not None) != is_resolved or (
            self.resolved_at is not None
        ) != is_resolved:
            raise ValueError("resolved_by and resolved_at are set exactly when there is a decision")
        if self.resolved_by is not None and self.resolved_by == self.requested_by:
            raise ValueError("a request cannot be resolved by the principal who made it")
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at is earlier than created_at")

        if (self.consumed_at is not None) != (self.status is ApprovalStatus.CONSUMED):
            raise ValueError("consumed_at is set exactly when the status is consumed")
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
