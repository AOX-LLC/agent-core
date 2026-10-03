"""The approval queue on SQLite or Postgres.

Resolving and consuming are compare-and-set updates (`... WHERE status = ?`), so of
two concurrent attempts exactly one wins. Every submission, resolution, consumption
and denied attempt writes an audit event; when the queue and its audit log share
a Database, in the same transaction as the change.
"""

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar
from uuid import UUID, uuid4

from pydantic import JsonValue

from aox_agent_core import _postgres_schema as layout
from aox_agent_core._canonical import canonical_json, sha256_of
from aox_agent_core._validation import STORED_RECORD
from aox_agent_core.approvals.policy import ApproverPolicy, RoleApproverPolicy
from aox_agent_core.approvals.types import (
    TTL_SECONDS_MAX,
    ApprovalRequest,
    ApprovalSide,
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
from aox_agent_core.context import RunContext
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    ConfigError,
    NotAuthorizedToResolveError,
    NotTheRequesterError,
)
from aox_agent_core.storage import (
    Database,
    Dialect,
    Session,
    TableName,
    bring_table_up_to_date,
    require_current_table,
)

ResultT = TypeVar("ResultT")

APPROVALS_TABLE: Final = "agent_core_approvals"
RUN_CONTEXT_COLUMN: Final = "run_context"

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
    reason TEXT,
    run_context TEXT,
    closed_at TEXT,
    delegates TEXT NOT NULL DEFAULT '[]'
)"""
# Columns added in 0.1.0a3, with their SQLite types.
ADDED_IN_A3: Final = {"closed_at": "TEXT", "delegates": "TEXT NOT NULL DEFAULT '[]'"}

_PENDING_INDEX_DDL = (
    f"CREATE INDEX agent_core_approvals_pending ON {APPROVALS_TABLE} (status, created_at, id)"
)

DEFAULT_PENDING_LIMIT = 100
# Pages read while a custom policy filters; larger than most limits, so a policy
# that rejects many requests needs few round trips.
PENDING_PAGE_SIZE = 500

_COLUMNS = (
    "id, action, summary, payload_sha256, requested_by, required_role, created_at, "
    "expires_at, status, decision, resolved_by, resolved_at, consumed_at, reason, run_context, "
    "delegates, closed_at"
)
DELEGATES_COLUMN: Final = "delegates"

_DENIAL_ERRORS: Final[Mapping[DenialReason, type[ApprovalError]]] = {
    DenialReason.NOT_HUMAN: NotAuthorizedToResolveError,
    DenialReason.MISSING_ROLE: NotAuthorizedToResolveError,
    DenialReason.SELF_APPROVAL: NotAuthorizedToResolveError,
    DenialReason.NOT_PENDING: ApprovalAlreadyResolvedError,
    DenialReason.UNKNOWN_ACTION: NotAuthorizedToResolveError,
    DenialReason.ROLE_MISMATCH: NotAuthorizedToResolveError,
    DenialReason.NOT_REQUESTER: NotAuthorizedToResolveError,
    DenialReason.EXPIRED: ApprovalExpiredError,
}


def approval_payload_hash(action: str, payload: Mapping[str, JsonValue]) -> str:
    """SHA-256 of the canonical JSON of {"action": action, "payload": payload}."""
    return sha256_of({"action": action, "payload": dict(payload)})


@dataclass
class _Outcome:
    """What one transaction decided, raised or returned after it commits."""

    request: ApprovalRequest | None = None
    error: ApprovalError | None = None
    events: list[AuditEvent] = field(default_factory=list)
    # How many requests an expiry batch stored as expired, and whether it was full.
    expired: int = 0
    batch_full: bool = False


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
        schema: str | None = None,
    ) -> None:
        self.database = database
        self._table = TableName.on(database, APPROVALS_TABLE, schema)
        self._audit_log = audit_log
        self._policy = policy if policy is not None else RoleApproverPolicy()
        self._clock = clock if clock is not None else _utc_now
        self._schema = self._table.schema or layout.DEFAULT_SCHEMA
        self._side: ApprovalSide | None = None

    async def side(self) -> ApprovalSide:
        """Which side this queue's connection acts for, checking the setup first.

        Raises ConfigError if the Postgres schema or roles are wrong; see
        _postgres_schema.check_connection.
        """
        return await self.database.run(self._prepare)

    def _prepare(self, session: Session) -> ApprovalSide:
        """Check the connection once per queue, upgrading a SQLite file in place."""
        if self._side is None:
            if session.dialect is Dialect.SQLITE:
                bring_table_up_to_date(session, APPROVALS_TABLE, ADDED_IN_A3)
                self._side = ApprovalSide.BOTH
            else:
                self._side = ApprovalSide(layout.check_connection(session, self._schema))
        return self._side

    def _require_side(self, session: Session, operation: str, side: ApprovalSide) -> None:
        actual = self._prepare(session)
        if actual not in (side, ApprovalSide.BOTH):
            raise ConfigError(
                f"This queue connects as the {actual.value} role, which cannot {operation}; "
                f"use a queue on the {side.value} role's connection."
            )

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
        delegates: Collection[str] = (),
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Queue a request that expires after ttl_seconds (at most TTL_SECONDS_MAX).

        The payload's hash is stored; the payload itself is not. `context`, the
        run asking, is stored on the request and its audit event. Only
        requested_by may consume the approval, unless `delegates` names other
        principals allowed to: an explicit choice, shown to the approver.
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
            run_context=context,
            delegates=frozenset(delegates),
        )

        def insert(session: Session) -> _Outcome:
            self._require_side(session, "submit requests", ApprovalSide.REQUESTER)
            _ensure_table(session)
            require_current_table(
                session, APPROVALS_TABLE, RUN_CONTEXT_COLUMN, schema=self._table.schema
            )
            session.execute(
                f"INSERT INTO {self._table.sql} ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _row_values(request),
            )
            event = _event(
                "approval.requested",
                requested_by.id,
                request,
                context,
                required_role=required_role,
                payload_sha256=request.payload_sha256,
            )
            if request.delegates:
                event = event.model_copy(
                    update={"payload": {**event.payload, "delegates": sorted(request.delegates)}}
                )
            return _Outcome(request=request, events=[event])

        return await self._write(insert)

    async def get(self, request_id: UUID) -> ApprovalRequest:
        """Return the request; raises ApprovalNotFoundError if there is none."""

        def read(session: Session) -> ApprovalRequest | None:
            self._prepare(session)
            return _load(session, self._table, request_id)

        request = await self.database.run(read)
        if request is None:
            raise ApprovalNotFoundError(f"No approval request {request_id}.")
        if request.status is ApprovalStatus.PENDING and request.is_expired(self._now()):
            # Reported as the sweep would store it, without writing anything.
            return request.model_copy(
                update={"status": ApprovalStatus.EXPIRED, "closed_at": request.expires_at}
            )
        return request

    async def list_pending(
        self,
        principal: Principal,
        *,
        limit: int = DEFAULT_PENDING_LIMIT,
        after: UUID | None = None,
    ) -> Sequence[ApprovalRequest]:
        """Up to `limit` pending, unexpired requests this principal may resolve, oldest first.

        Requests are ordered by creation (created_at, then id). To read the next
        page, pass the last request's id as `after`; listing resumes right after
        it. An unknown `after` raises ApprovalNotFoundError.

        The policy decides each request. With the default RoleApproverPolicy the
        database also narrows by role and requester, which keeps a large queue
        cheap; with any other policy the queue is read page by page until `limit`
        requests pass or none are left, so a custom policy sees every candidate.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        uses_default_policy = type(self._policy) is RoleApproverPolicy
        if uses_default_policy and (
            principal.kind is not PrincipalKind.HUMAN or not principal.roles
        ):
            return []
        now = self._now()
        narrowed_to = principal if uses_default_policy else None
        page_size = limit if uses_default_policy else max(limit, PENDING_PAGE_SIZE)

        eligible: list[ApprovalRequest] = []
        resume_after = await self.get(after) if after is not None else None

        def read_pages(session: Session) -> bool:
            """Read pages until `limit` requests pass; return whether more pages remain."""
            nonlocal resume_after
            self._prepare(session)
            while len(eligible) < limit:
                page = _load_pending_page(
                    session,
                    self._table,
                    now=now,
                    after=resume_after,
                    narrowed_to=narrowed_to,
                    limit=page_size,
                )
                eligible.extend(
                    request
                    for request in page
                    if self._policy.evaluate(principal, request, now=now).allowed
                )
                if len(page) < page_size:
                    return False
                resume_after = page[-1]
                if session.dialect is Dialect.SQLITE:
                    return len(eligible) < limit
            return False

        # Postgres reads never block writers, so all pages share one transaction.
        # A SQLite read holds a lock that makes writers wait, so there each page is
        # its own short transaction.
        while await self.database.run(read_pages):
            pass
        return eligible[:limit]

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Approve or reject a pending request, once.

        Raises NotAuthorizedToResolveError if the ApproverPolicy denies it,
        ApprovalAlreadyResolvedError if it is no longer pending,
        ApprovalExpiredError if it has expired, and ApprovalNotFoundError if it
        does not exist. Each of those is audited first, with `context` or, without
        one, the request's own.
        """

        def decide(session: Session) -> _Outcome:
            self._require_side(session, "decide requests", ApprovalSide.APPROVER)
            request = _load(session, self._table, request_id)
            if request is None:
                return _denied(
                    ApprovalNotFoundError(f"No approval request {request_id}."),
                    _missing_event("approval.resolve_denied", principal.id, request_id, context),
                )
            event_context = context if context is not None else request.run_context
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
                        event_context,
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
                f"UPDATE {self._table.sql} SET status = ?, decision = ?, resolved_by = ?, "
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
                        event_context,
                        decision=decision.value,
                        reason=DenialReason.NOT_PENDING.value,
                    ),
                )
            event = _event(
                "approval.resolved", principal.id, resolved, event_context, decision=decision.value
            )
            return _Outcome(request=resolved, events=[event])

        return await self._write(decide)

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Call right before acting. Atomically moves an approved request to CONSUMED.

        `principal` is whoever is about to act; the audit event names them, with
        `context` or, without one, the request's own. It must be the principal
        who requested the approval or one of the request's delegates, or
        NotTheRequesterError is raised. One
        approval authorizes one run. Raises ApprovalPayloadMismatchError if the
        action or payload differ from what was approved, ApprovalNotGrantedError if
        the request is pending or was rejected, ApprovalAlreadyResolvedError if it
        was already consumed or cancelled, and ApprovalExpiredError if it expired.
        """
        presented_hash = approval_payload_hash(action, payload)

        def use(session: Session) -> _Outcome:
            self._require_side(session, "consume approvals", ApprovalSide.REQUESTER)
            request = _load(session, self._table, request_id)
            if request is None:
                return _denied(
                    ApprovalNotFoundError(f"No approval request {request_id}."),
                    _missing_event("approval.consume_denied", principal.id, request_id, context),
                )
            event_context = context if context is not None else request.run_context
            now = self._now()
            refusal = _consume_refusal(request, presented_hash, now, principal)
            if refusal is not None:
                error, reason = refusal
                return _denied(
                    error,
                    _event(
                        "approval.consume_denied",
                        principal.id,
                        request,
                        event_context,
                        reason=reason,
                    ),
                )

            consumed = ApprovalRequest.model_validate(
                {**request.model_dump(), "status": ApprovalStatus.CONSUMED, "consumed_at": now}
            )
            changed = session.execute_count(
                f"UPDATE {self._table.sql} SET status = ?, consumed_at = ? "
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
                    _event(
                        "approval.consume_denied",
                        principal.id,
                        request,
                        event_context,
                        reason="not_open",
                    ),
                )
            event = _event("approval.consumed", principal.id, consumed, event_context)
            return _Outcome(request=consumed, events=[event])

        return await self._write(use)

    async def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Withdraw a pending request. Only the principal who submitted it may.

        Delegates may consume an approval but never cancel the request. `reason`
        is a short explanation recorded in the audit event (never personal data).
        Raises NotTheRequesterError for anyone else, ApprovalAlreadyResolvedError
        if the request is no longer pending, ApprovalExpiredError if it has
        expired, and ApprovalNotFoundError if it does not exist; each is audited.
        """
        if reason is not None and not 0 < len(reason) <= 500:
            raise ValueError("reason must be 1 to 500 characters")
        details = {"cancel_reason": reason} if reason is not None else {}

        def withdraw(session: Session) -> _Outcome:
            self._require_side(session, "cancel requests", ApprovalSide.REQUESTER)
            request = _load(session, self._table, request_id)
            if request is None:
                return _denied(
                    ApprovalNotFoundError(f"No approval request {request_id}."),
                    _missing_event("approval.cancel_denied", principal.id, request_id, context),
                )
            event_context = context if context is not None else request.run_context
            now = self._now()
            refusal = _cancel_refusal(request, principal, now)
            if refusal is not None:
                error, denial = refusal
                return _denied(
                    error,
                    _event(
                        "approval.cancel_denied",
                        principal.id,
                        request,
                        event_context,
                        reason=denial,
                    ),
                )
            cancelled = request.model_copy(
                update={"status": ApprovalStatus.CANCELLED, "closed_at": now}
            )
            if not _close(session, self._table, request, ApprovalStatus.CANCELLED, now):
                return _denied(
                    ApprovalAlreadyResolvedError(f"Request {request_id} was resolved meanwhile."),
                    _event(
                        "approval.cancel_denied",
                        principal.id,
                        request,
                        event_context,
                        reason=DenialReason.NOT_PENDING.value,
                    ),
                )
            event = _event("approval.cancelled", principal.id, cancelled, event_context, **details)
            return _Outcome(request=cancelled, events=[event])

        return await self._write(withdraw)

    async def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = 500
    ) -> int:
        """Store EXPIRED on every pending request whose lifetime is over; return how many.

        Reads already report such requests as expired, so this only makes the
        stored state match, with one approval.expired audit event per request
        naming `principal` as the actor. Either side may run it. It works in
        batches of `limit`, each its own transaction. On Postgres a request
        counts as due only once the database's clock agrees, as the guard does.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        moment = now if now is not None else self._now()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")

        def sweep(session: Session) -> _Outcome:
            self._prepare(session)
            if not _table_exists(session, self._table):
                return _Outcome()
            due = _load_due(session, self._table, moment, limit)
            events = []
            for request in due:
                if _close(session, self._table, request, ApprovalStatus.EXPIRED, moment):
                    expired = request.model_copy(
                        update={"status": ApprovalStatus.EXPIRED, "closed_at": moment}
                    )
                    # The sweep belongs to no run; the event carries the request's own.
                    events.append(
                        _event("approval.expired", principal.id, expired, request.run_context)
                    )
            return _Outcome(events=events, expired=len(events), batch_full=len(due) == limit)

        total = 0
        while True:
            outcome = await self._write_outcome(sweep)
            total += outcome.expired
            if not outcome.batch_full:
                return total

    async def _write(self, work: Callable[[Session], _Outcome]) -> ApprovalRequest:
        """Run `work` as _write_outcome does, then return its request or raise its error."""
        outcome = await self._write_outcome(work)
        if outcome.error is not None:
            raise outcome.error
        if outcome.request is None:
            raise AssertionError("an approval transaction must return a request or an error")
        return outcome.request

    async def _write_outcome(self, work: Callable[[Session], _Outcome]) -> _Outcome:
        """Run `work` in a write transaction and audit its events.

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
        return outcome


def _consume_refusal(
    request: ApprovalRequest, presented_hash: str, now: datetime, principal: Principal
) -> tuple[ApprovalError, str] | None:
    """Why this request cannot authorize a run now, or None if it can."""
    request_id = request.id
    if principal.id != request.requested_by and principal.id not in request.delegates:
        return (
            NotTheRequesterError(
                f"Request {request_id} was made by {request.requested_by}; only they or a "
                "delegate it named may use it."
            ),
            DenialReason.NOT_REQUESTER.value,
        )
    if presented_hash != request.payload_sha256:
        return (
            ApprovalPayloadMismatchError(
                f"Request {request_id} approved a different action or payload."
            ),
            "payload_mismatch",
        )
    if request.status is ApprovalStatus.EXPIRED:
        return ApprovalExpiredError(f"Request {request_id} expired before it was used."), "expired"
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


def _cancel_refusal(
    request: ApprovalRequest, principal: Principal, now: datetime
) -> tuple[ApprovalError, str] | None:
    """Why this principal cannot cancel this request now, or None if it can."""
    if principal.id != request.requested_by:
        return (
            NotTheRequesterError(
                f"Request {request.id} was made by {request.requested_by}; only they may cancel it."
            ),
            DenialReason.NOT_REQUESTER.value,
        )
    if request.status is ApprovalStatus.EXPIRED or (
        request.status is ApprovalStatus.PENDING and request.is_expired(now)
    ):
        return ApprovalExpiredError(f"Request {request.id} has expired."), "expired"
    if request.status is not ApprovalStatus.PENDING:
        return (
            ApprovalAlreadyResolvedError(f"Request {request.id} is {request.status.value}."),
            DenialReason.NOT_PENDING.value,
        )
    return None


def _close(
    session: Session,
    table: TableName,
    request: ApprovalRequest,
    status: ApprovalStatus,
    now: datetime,
) -> bool:
    """Move a pending request to `status` (expired or cancelled); False if it moved meanwhile."""
    changed = session.execute_count(
        f"UPDATE {table.sql} SET status = ?, closed_at = ? WHERE id = ? AND status = ?",
        (status.value, canonical_timestamp(now), str(request.id), ApprovalStatus.PENDING.value),
    )
    return changed == 1


# The database's clock as a canonical timestamp, so the sweep picks only what the
# guard agrees has expired, even when the application's clock runs ahead.
_POSTGRES_NOW_TEXT = (
    "to_char(statement_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
)


def _load_due(
    session: Session, table: TableName, now: datetime, limit: int
) -> list[ApprovalRequest]:
    """Up to `limit` pending requests whose lifetime ended by `now`, oldest expiry first."""
    database_clock = (
        f" AND expires_at <= {_POSTGRES_NOW_TEXT}" if session.dialect is Dialect.POSTGRES else ""
    )
    rows = session.execute(
        f"SELECT {_COLUMNS} FROM {table.sql} WHERE status = ? AND expires_at <= ?"
        f"{database_clock} ORDER BY expires_at, id LIMIT ?",
        (ApprovalStatus.PENDING.value, canonical_timestamp(now), limit),
    )
    return [_request_from_row(row) for row in rows]


def _denied(error: ApprovalError, event: AuditEvent) -> _Outcome:
    return _Outcome(error=error, events=[event])


def _event(
    action: str,
    actor_id: str,
    request: ApprovalRequest,
    context: RunContext | None,
    **details: str,
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_id=actor_id,
        subject_id=str(request.id),
        payload={"approval_action": request.action, **details},
        context=context,
    )


def _missing_event(
    action: str, actor_id: str | None, request_id: UUID, context: RunContext | None
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_id=actor_id or "unknown",
        subject_id=str(request_id),
        payload={"reason": "not_found"},
        context=context,
    )


def _ensure_table(session: Session) -> None:
    # On Postgres the owner role installs the table; the app role cannot create it.
    if session.dialect is Dialect.SQLITE:
        session.execute(_TABLE_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        session.execute(_PENDING_INDEX_DDL.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))


def _table_exists(session: Session, table: TableName) -> bool:
    """Whether the table exists; ConfigError if it is a 0.1.0a1 table without run_context."""
    # Reads before the first submit see "no table", which means "no requests".
    if session.dialect is Dialect.SQLITE:
        exists = bool(
            session.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (APPROVALS_TABLE,),
            )
        )
    else:
        exists = bool(session.execute("SELECT to_regclass(?) IS NOT NULL", (table.sql,))[0][0])
    if exists:
        require_current_table(session, APPROVALS_TABLE, RUN_CONTEXT_COLUMN, schema=table.schema)
    return exists


def _load(session: Session, table: TableName, request_id: UUID) -> ApprovalRequest | None:
    if not _table_exists(session, table):
        return None
    rows = session.execute(f"SELECT {_COLUMNS} FROM {table.sql} WHERE id = ?", (str(request_id),))
    return _request_from_row(rows[0]) if rows else None


def _load_pending_page(
    session: Session,
    table: TableName,
    *,
    now: datetime,
    after: ApprovalRequest | None,
    narrowed_to: Principal | None,
    limit: int,
) -> list[ApprovalRequest]:
    """One page of pending, unexpired requests after `after`, in (created_at, id) order.

    With `narrowed_to`, only requests that principal could resolve under the
    default policy: a role it holds, and not its own.
    """
    if not _table_exists(session, table):
        return []
    # Canonical timestamps are fixed-width UTC strings, so they compare as text.
    conditions = ["status = ?", "expires_at > ?"]
    parameters: list[Any] = [ApprovalStatus.PENDING.value, canonical_timestamp(now)]
    if after is not None:
        # A row-value comparison lets the (status, created_at, id) index seek straight
        # to the page start, even when many requests share a created_at.
        conditions.append("(created_at, id) > (?, ?)")
        parameters += [canonical_timestamp(after.created_at), str(after.id)]
    if narrowed_to is not None:
        roles = sorted(narrowed_to.roles)
        conditions.append(f"required_role IN ({', '.join('?' for _ in roles)})")
        conditions.append("requested_by <> ?")
        parameters += [*roles, narrowed_to.id]
    rows = session.execute(
        f"SELECT {_COLUMNS} FROM {table.sql} WHERE {' AND '.join(conditions)} "
        "ORDER BY created_at, id LIMIT ?",
        (*parameters, limit),
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
        (
            canonical_json(request.run_context.as_json()).decode("utf-8")
            if request.run_context is not None
            else None
        ),
        json.dumps(sorted(request.delegates)),
        timestamp(request.closed_at),
    )


def _request_from_row(row: tuple[Any, ...]) -> ApprovalRequest:
    # NULL columns are dropped so the model's defaults apply.
    names = [name.strip() for name in _COLUMNS.split(",")]
    fields = {name: value for name, value in zip(names, row, strict=True) if value is not None}
    if RUN_CONTEXT_COLUMN in fields:
        fields[RUN_CONTEXT_COLUMN] = json.loads(fields[RUN_CONTEXT_COLUMN])
    fields[DELEGATES_COLUMN] = json.loads(fields.get(DELEGATES_COLUMN, "[]"))
    return ApprovalRequest.model_validate(fields, context={STORED_RECORD: True})


def _utc_now() -> datetime:
    return datetime.now(UTC)
