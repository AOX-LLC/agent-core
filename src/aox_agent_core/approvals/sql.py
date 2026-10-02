"""The approval queue on SQLite or Postgres.

Resolving and consuming are compare-and-set updates (`... WHERE status = ?`), so of
two concurrent attempts exactly one wins. Every submission, resolution, consumption
and denied attempt writes an audit event; when the queue and its audit log share
a Database, in the same transaction as the change.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final, TypeVar
from uuid import UUID, uuid4

from pydantic import JsonValue

from aox_agent_core._canonical import sha256_of
from aox_agent_core.approvals.policy import ApproverPolicy, RoleApproverPolicy
from aox_agent_core.approvals.types import (
    TTL_SECONDS_MAX,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    DenialReason,
    Principal,
    PrincipalKind,
)
from aox_agent_core.audit.chain import canonical_timestamp
from aox_agent_core.audit.log import AuditLog
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.audit.types import AuditEvent
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    NotAuthorizedToResolveError,
)
from aox_agent_core.storage import Database, Dialect, Session

ResultT = TypeVar("ResultT")

APPROVALS_TABLE: Final = "agent_core_approvals"

_TABLE_DDL = f"""
CREATE TABLE {APPROVALS_TABLE} (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    required_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    consumed_at TEXT,
    reason TEXT
)"""

_PENDING_INDEX_DDL = (
    f"CREATE INDEX agent_core_approvals_pending ON {APPROVALS_TABLE} (status, created_at)"
)

SCHEMA: Final = (
    _TABLE_DDL,
    _PENDING_INDEX_DDL,
    f"REVOKE ALL ON {APPROVALS_TABLE} FROM PUBLIC",
)

DEFAULT_PENDING_LIMIT = 100

_COLUMNS = (
    "id, action, summary, payload_sha256, requested_by, required_role, created_at, "
    "expires_at, status, decision, resolved_by, resolved_at, consumed_at, reason"
)

_DENIAL_ERRORS: Final[Mapping[DenialReason, type[ApprovalError]]] = {
    DenialReason.NOT_HUMAN: NotAuthorizedToResolveError,
    DenialReason.MISSING_ROLE: NotAuthorizedToResolveError,
    DenialReason.SELF_APPROVAL: NotAuthorizedToResolveError,
    DenialReason.NOT_PENDING: ApprovalAlreadyResolvedError,
    DenialReason.EXPIRED: ApprovalExpiredError,
}


def postgres_grants(app_role: str) -> tuple[str, ...]:
    """The app role reads, adds and updates requests; it never deletes them."""
    return (f'GRANT SELECT, INSERT, UPDATE ON {APPROVALS_TABLE} TO "{app_role}"',)


def approval_payload_hash(action: str, payload: Mapping[str, JsonValue]) -> str:
    """SHA-256 of the canonical JSON of {"action": action, "payload": payload}."""
    return sha256_of({"action": action, "payload": dict(payload)})


@dataclass
class _Outcome:
    """What one transaction decided, raised or returned after it commits."""

    request: ApprovalRequest | None = None
    error: ApprovalError | None = None
    events: list[AuditEvent] = field(default_factory=list)


class SQLApprovalQueue:
    """The ApprovalQueue protocol on a Database.

    The policy (RoleApproverPolicy by default) is applied inside resolve(); a
    caller cannot skip it. A denied attempt is audited and committed before its
    error is raised. consume() is called right before acting: it moves an approved
    request to CONSUMED, so one approval authorizes exactly one run, and its audit
    event names the principal about to act.
    """

    def __init__(
        self,
        database: Database,
        *,
        audit_log: AuditLog,
        policy: ApproverPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._audit_log = audit_log
        self._policy = policy if policy is not None else RoleApproverPolicy()
        self._clock = clock if clock is not None else _utc_now

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("the approval queue's clock must return timezone-aware datetimes")
        return now

    async def submit(
        self,
        *,
        action: str,
        summary: str,
        payload: Mapping[str, JsonValue],
        requested_by: Principal,
        required_role: str,
        ttl_seconds: int,
    ) -> ApprovalRequest:
        """Queue a request that expires after ttl_seconds (at most TTL_SECONDS_MAX).

        The payload's hash is stored; the payload itself is not.
        """
        if not 0 < ttl_seconds <= TTL_SECONDS_MAX:
            raise ValueError(f"ttl_seconds must be 1 to {TTL_SECONDS_MAX}, got {ttl_seconds}")
        now = self._now()
        request = ApprovalRequest(
            id=uuid4(),
            action=action,
            summary=summary,
            payload_sha256=approval_payload_hash(action, payload),
            requested_by=requested_by.id,
            required_role=required_role,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

        def insert(session: Session) -> _Outcome:
            _ensure_table(session)
            session.execute(
                f"INSERT INTO {APPROVALS_TABLE} ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _row_values(request),
            )
            event = _event(
                "approval.requested",
                requested_by.id,
                request,
                required_role=required_role,
                payload_sha256=request.payload_sha256,
            )
            return _Outcome(request=request, events=[event])

        return await self._write(insert)

    async def get(self, request_id: UUID) -> ApprovalRequest:
        """Return the request; raises ApprovalNotFoundError if there is none."""
        request = await self.database.run(lambda session: _load(session, request_id))
        if request is None:
            raise ApprovalNotFoundError(f"No approval request {request_id}.")
        return request

    async def list_pending(
        self, principal: Principal, *, limit: int = DEFAULT_PENDING_LIMIT
    ) -> Sequence[ApprovalRequest]:
        """Up to `limit` pending, unexpired requests this principal may resolve, oldest first.

        The database narrows by status, expiry, role and requester; the policy then
        has the final say on each request.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        if principal.kind is not PrincipalKind.HUMAN or not principal.roles:
            return []
        now = self._now()
        pending = await self.database.run(
            partial(_load_pending, principal=principal, now=now, limit=limit)
        )
        return [
            request
            for request in pending
            if self._policy.evaluate(principal, request, now=now).allowed
        ]

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
    ) -> ApprovalRequest:
        """Approve or reject a pending request, once.

        Raises NotAuthorizedToResolveError if the ApproverPolicy denies it,
        ApprovalAlreadyResolvedError if it is no longer pending,
        ApprovalExpiredError if it has expired, and ApprovalNotFoundError if it
        does not exist. Each of those is audited first.
        """

        def decide(session: Session) -> _Outcome:
            request = _load(session, request_id)
            if request is None:
                return _denied(
                    ApprovalNotFoundError(f"No approval request {request_id}."),
                    _missing_event("approval.resolve_denied", principal.id, request_id),
                )
            now = self._now()
            verdict = self._policy.evaluate(principal, request, now=now)
            if not verdict.allowed:
                # A custom policy that denies without a reason is still a denial.
                denial = verdict.reason.value if verdict.reason is not None else "denied"
                error_type = (
                    _DENIAL_ERRORS[verdict.reason]
                    if verdict.reason is not None
                    else NotAuthorizedToResolveError
                )
                return _denied(
                    error_type(f"Request {request_id} cannot be resolved: {denial}."),
                    _event(
                        "approval.resolve_denied",
                        principal.id,
                        request,
                        decision=decision.value,
                        reason=denial,
                    ),
                )

            status = (
                ApprovalStatus.APPROVED if decision is Decision.APPROVE else ApprovalStatus.REJECTED
            )
            resolved = ApprovalRequest.model_validate(
                {
                    **request.model_dump(),
                    "status": status,
                    "decision": decision,
                    "resolved_by": principal.id,
                    "resolved_at": now,
                    "reason": reason,
                }
            )
            changed = session.execute_count(
                f"UPDATE {APPROVALS_TABLE} SET status = ?, decision = ?, resolved_by = ?, "
                "resolved_at = ?, reason = ? WHERE id = ? AND status = ?",
                (
                    status.value,
                    decision.value,
                    principal.id,
                    canonical_timestamp(now),
                    reason,
                    str(request_id),
                    ApprovalStatus.PENDING.value,
                ),
            )
            if changed != 1:
                return _denied(
                    ApprovalAlreadyResolvedError(f"Request {request_id} was resolved meanwhile."),
                    _event(
                        "approval.resolve_denied",
                        principal.id,
                        request,
                        decision=decision.value,
                        reason=DenialReason.NOT_PENDING.value,
                    ),
                )
            event = _event("approval.resolved", principal.id, resolved, decision=decision.value)
            return _Outcome(request=resolved, events=[event])

        return await self._write(decide)

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
    ) -> ApprovalRequest:
        """Call right before acting. Atomically moves an approved request to CONSUMED.

        `principal` is whoever is about to act; the audit event names them. One
        approval authorizes one run. Raises ApprovalPayloadMismatchError if the
        action or payload differ from what was approved, ApprovalNotGrantedError if
        the request is pending or was rejected, ApprovalAlreadyResolvedError if it
        was already consumed or cancelled, and ApprovalExpiredError if it expired.
        """
        presented_hash = approval_payload_hash(action, payload)

        def use(session: Session) -> _Outcome:
            request = _load(session, request_id)
            if request is None:
                return _denied(
                    ApprovalNotFoundError(f"No approval request {request_id}."),
                    _missing_event("approval.consume_denied", principal.id, request_id),
                )
            now = self._now()
            refusal = _consume_refusal(request, presented_hash, now)
            if refusal is not None:
                error, reason = refusal
                return _denied(
                    error, _event("approval.consume_denied", principal.id, request, reason=reason)
                )

            consumed = ApprovalRequest.model_validate(
                {**request.model_dump(), "status": ApprovalStatus.CONSUMED, "consumed_at": now}
            )
            changed = session.execute_count(
                f"UPDATE {APPROVALS_TABLE} SET status = ?, consumed_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    ApprovalStatus.CONSUMED.value,
                    canonical_timestamp(now),
                    str(request_id),
                    ApprovalStatus.APPROVED.value,
                ),
            )
            if changed != 1:
                return _denied(
                    ApprovalAlreadyResolvedError(f"Request {request_id} was used meanwhile."),
                    _event("approval.consume_denied", principal.id, request, reason="not_open"),
                )
            event = _event("approval.consumed", principal.id, consumed)
            return _Outcome(request=consumed, events=[event])

        return await self._write(use)

    async def _write(self, work: Callable[[Session], _Outcome]) -> ApprovalRequest:
        """Run `work` in a write transaction, audit its events, then return or raise.

        When the audit log is on the same database the events are written in the
        same transaction, so the change and its record commit together or not at
        all. An audit log elsewhere is written right after the commit; that is best
        effort, since a failure then cannot undo the change.
        """
        audit_log = self._audit_log
        checked_log = audit_log if isinstance(audit_log, SQLAuditLog) else None
        shares_database = checked_log is not None and checked_log.database.same_database(
            self.database
        )

        def in_transaction(session: Session) -> _Outcome:
            outcome = work(session)
            # Checked before the commit, so an event the log would refuse stops the change.
            checked_events = (
                [checked_log.checked_event(event) for event in outcome.events]
                if checked_log is not None
                else outcome.events
            )
            if shares_database and checked_log is not None:
                for event in checked_events:
                    checked_log.append_in(session, event)
            return outcome

        outcome = await self.database.run(in_transaction, write=True)
        if not shares_database:
            for event in outcome.events:
                await audit_log.append(event)
        if outcome.error is not None:
            raise outcome.error
        if outcome.request is None:
            raise AssertionError("an approval transaction must return a request or an error")
        return outcome.request


def _consume_refusal(
    request: ApprovalRequest, presented_hash: str, now: datetime
) -> tuple[ApprovalError, str] | None:
    """Why this request cannot authorize a run now, or None if it can."""
    request_id = request.id
    if presented_hash != request.payload_sha256:
        return (
            ApprovalPayloadMismatchError(
                f"Request {request_id} approved a different action or payload."
            ),
            "payload_mismatch",
        )
    if request.status in {ApprovalStatus.PENDING, ApprovalStatus.REJECTED}:
        return (
            ApprovalNotGrantedError(f"Request {request_id} is {request.status.value}."),
            "not_granted",
        )
    if request.status is not ApprovalStatus.APPROVED:
        return (
            ApprovalAlreadyResolvedError(f"Request {request_id} is {request.status.value}."),
            "not_open",
        )
    if request.is_expired(now):
        return ApprovalExpiredError(f"Request {request_id} expired before it was used."), "expired"
    return None


def _denied(error: ApprovalError, event: AuditEvent) -> _Outcome:
    return _Outcome(error=error, events=[event])


def _event(action: str, actor_id: str, request: ApprovalRequest, **details: str) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_id=actor_id,
        subject_id=str(request.id),
        payload={"approval_action": request.action, **details},
    )


def _missing_event(action: str, actor_id: str | None, request_id: UUID) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_id=actor_id or "unknown",
        subject_id=str(request_id),
        payload={"reason": "not_found"},
    )


def _ensure_table(session: Session) -> None:
    # On Postgres the owner role installs the table; the app role cannot create it.
    if session.dialect is Dialect.SQLITE:
        session.execute(_TABLE_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        session.execute(_PENDING_INDEX_DDL.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))


def _table_exists(session: Session) -> bool:
    # Reads before the first submit see "no table", which means "no requests".
    if session.dialect is Dialect.SQLITE:
        return bool(
            session.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (APPROVALS_TABLE,),
            )
        )
    return bool(session.execute("SELECT to_regclass(?) IS NOT NULL", (APPROVALS_TABLE,))[0][0])


def _load(session: Session, request_id: UUID) -> ApprovalRequest | None:
    if not _table_exists(session):
        return None
    rows = session.execute(
        f"SELECT {_COLUMNS} FROM {APPROVALS_TABLE} WHERE id = ?", (str(request_id),)
    )
    return _request_from_row(rows[0]) if rows else None


def _load_pending(
    session: Session, *, principal: Principal, now: datetime, limit: int
) -> list[ApprovalRequest]:
    if not _table_exists(session):
        return []
    roles = sorted(principal.roles)
    role_placeholders = ", ".join("?" for _ in roles)
    # Canonical timestamps are fixed-width UTC strings, so they compare as text.
    rows = session.execute(
        f"SELECT {_COLUMNS} FROM {APPROVALS_TABLE} "
        f"WHERE status = ? AND expires_at > ? AND required_role IN ({role_placeholders}) "
        "AND requested_by <> ? ORDER BY created_at LIMIT ?",
        (
            ApprovalStatus.PENDING.value,
            canonical_timestamp(now),
            *roles,
            principal.id,
            limit,
        ),
    )
    return [_request_from_row(row) for row in rows]


def _row_values(request: ApprovalRequest) -> tuple[Any, ...]:
    def timestamp(moment: datetime | None) -> str | None:
        return canonical_timestamp(moment) if moment is not None else None

    return (
        str(request.id),
        request.action,
        request.summary,
        request.payload_sha256,
        request.requested_by,
        request.required_role,
        timestamp(request.created_at),
        timestamp(request.expires_at),
        request.status.value,
        request.decision.value if request.decision is not None else None,
        request.resolved_by,
        timestamp(request.resolved_at),
        timestamp(request.consumed_at),
        request.reason,
    )


def _request_from_row(row: tuple[Any, ...]) -> ApprovalRequest:
    # NULL columns are dropped so the model's defaults apply.
    names = [name.strip() for name in _COLUMNS.split(",")]
    return ApprovalRequest.model_validate(
        {name: value for name, value in zip(names, row, strict=True) if value is not None}
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
