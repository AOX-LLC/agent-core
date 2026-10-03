"""Approval requests, the people who resolve them, and the policy's verdict."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    JsonValue,
    StringConstraints,
    model_validator,
)

from aox_agent_core._model import ActionName, FrozenModel, PrincipalId, Sha256Hex
from aox_agent_core._text import require_safe_text
from aox_agent_core.context import RunContext

RoleName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
# Free text for people to read: no control, bidirectional or other format characters.
ShortText = Annotated[
    str, StringConstraints(min_length=1, max_length=500), AfterValidator(require_safe_text)
]

TTL_SECONDS_MAX = 7 * 24 * 60 * 60
MAX_DELEGATES = 16


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

    A pending request becomes CANCELLED when its requester withdraws it, and
    EXPIRED when expire_due() stores its expiry. Expiry does not wait for that
    sweep: it is judged from expires_at whenever a request is read, resolved or
    used, so a pending or approved request past its lifetime reads as EXPIRED either
    way. An approval that lapsed unused is EXPIRED and keeps its decision (approve),
    resolved_by and resolved_at.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalSide(StrEnum):
    """Which side of the approval queue a connection acts for.

    On Postgres the requester role (the agent side) submits, consumes and
    cancels, and the approver role (the decision side) approves and rejects;
    the database enforces it. SQLite has no roles, so a SQLite queue is BOTH and
    the library's checks are all there is: anyone who can write the file is
    trusted with everything.
    """

    REQUESTER = "requester"
    APPROVER = "approver"
    BOTH = "both"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


# The decisions each status allows. A request has resolved_by and resolved_at exactly when
# it has a decision. An approval that lapsed unused is EXPIRED and keeps its decision.
_DECISIONS_FOR_STATUS: Mapping[ApprovalStatus, frozenset[Decision | None]] = {
    ApprovalStatus.PENDING: frozenset({None}),
    ApprovalStatus.APPROVED: frozenset({Decision.APPROVE}),
    ApprovalStatus.REJECTED: frozenset({Decision.REJECT}),
    ApprovalStatus.CONSUMED: frozenset({Decision.APPROVE}),
    ApprovalStatus.EXPIRED: frozenset({None, Decision.APPROVE}),
    ApprovalStatus.CANCELLED: frozenset({None}),
}


class ApprovalRequest(FrozenModel):
    """A request for a human to approve one run of one action.

    payload_sha256 is the SHA-256 of the canonical JSON (keys sorted, no
    insignificant whitespace, UTF-8) of {"action": action, "payload": payload}.
    Hashing the action with the payload means an approval for one action can
    never authorize a different action that happens to share its payload.
    An approval authorizes a single run: using it moves it to CONSUMED.
    run_context is the run that asked for the approval, if the caller named one.

    Only requested_by may consume it, unless it names delegates: principals the
    requester allowed to consume in its place, fixed when it is submitted and
    shown to whoever decides it.

    `summary` is free text the requester wrote. It is NOT bound by payload_sha256:
    never decide from it alone. `payload` is the exact payload the hash covers,
    present only when the requester asked for it to be stored (include_payload);
    the queue has checked it against payload_sha256 before returning the request,
    so what an approver sees there is what the hash binds. It is None when the
    requester did not store it, and on what consume(), cancel() and expire_due()
    work on: they check nothing about a stored payload, so they return none.

    `payload_purged_at` tells a payload that was dropped from one never stored: after
    purge_payloads() on a finished request the payload is gone, this holds when, and
    payload_sha256 still binds what it was. A request that never stored a payload has
    both None.
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
    closed_at: AwareDatetime | None = None
    reason: ShortText | None = None
    run_context: RunContext | None = None
    delegates: frozenset[PrincipalId] = frozenset()
    payload: dict[str, JsonValue] | None = None
    payload_purged_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _few_delegates(self) -> Self:
        if len(self.delegates) > MAX_DELEGATES:
            raise ValueError(f"at most {MAX_DELEGATES} delegates")
        return self

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
        if self.decision not in _DECISIONS_FOR_STATUS[self.status]:
            raise ValueError(f"status {self.status.value} does not match decision {self.decision}")

        is_resolved = self.decision is not None
        if (self.resolved_by is not None) != is_resolved or (
            self.resolved_at is not None
        ) != is_resolved:
            raise ValueError("resolved_by and resolved_at are set exactly when there is a decision")
        if self.resolved_by is not None and self.resolved_by == self.requested_by:
            raise ValueError("a request cannot be resolved by the principal who made it")
        if self.payload_purged_at is not None and (
            self.payload is not None
            or self.status
            not in {
                ApprovalStatus.CONSUMED,
                ApprovalStatus.REJECTED,
                ApprovalStatus.CANCELLED,
                ApprovalStatus.EXPIRED,
            }
        ):
            raise ValueError("only a finished request has its payload purged, and then has none")
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at is earlier than created_at")

        if (self.consumed_at is not None) != (self.status is ApprovalStatus.CONSUMED):
            raise ValueError("consumed_at is set exactly when the status is consumed")
        is_closed = self.status in {ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED}
        if (self.closed_at is not None) != is_closed:
            raise ValueError("closed_at is set exactly when the status is expired or cancelled")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class DenialReason(StrEnum):
    """Why a principal may not resolve a request. Recorded in the audit log."""

    NOT_HUMAN = "not_human"
    MISSING_ROLE = "missing_role"
    SELF_APPROVAL = "self_approval"
    DELEGATE_APPROVAL = "delegate_approval"
    LOGIN_BINDING = "login_binding"
    NOT_PENDING = "not_pending"
    NOT_REQUESTER = "not_requester"
    UNKNOWN_ACTION = "unknown_action"
    ROLE_MISMATCH = "role_mismatch"
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
