"""The approval queue on SQLite or Postgres.

Resolving and consuming are compare-and-set updates (`... WHERE status = ?`), so of
two concurrent attempts exactly one wins. Every submission, resolution, consumption
and denied attempt writes an audit event; when the queue and its audit log share
a Database, in the same transaction as the change.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

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
from aox_agent_core.audit.types import AuditEvent, check_payload, exceeds_depth
from aox_agent_core.context import RunContext
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    ApprovalPayloadRejectedError,
    AuditPayloadRejectedError,
    ConfigError,
    NotAuthorizedToResolveError,
    NotTheRequesterError,
)
from aox_agent_core.replay.scrub import PatternScrubber, Scrubber
from aox_agent_core.storage import (
    Database,
    Dialect,
    Session,
    TableName,
    bring_table_up_to_date,
    driver_errors,
    require_current_table,
)

ResultT = TypeVar("ResultT")

APPROVALS_TABLE: Final = layout.APPROVALS_TABLE
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
    delegates TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT
)"""
# Columns added in 0.1.0a3 and 0.1.0a4, with their SQLite types.
ADDED_COLUMNS: Final = {
    "closed_at": "TEXT",
    "delegates": "TEXT NOT NULL DEFAULT '[]'",
    "payload_json": "TEXT",
}

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
    "delegates, closed_at, payload_json"
)
DELEGATES_COLUMN: Final = "delegates"
PAYLOAD_COLUMN: Final = "payload_json"
# Seconds an independent audit write waits for the append lock before falling back.
DENIAL_LOCK_TIMEOUT: Final = "2s"
# Seconds an independent audit write waits for a pooled connection before falling back.
DENIAL_ACQUIRE_TIMEOUT: Final = 2.0
# At most one warning this often about requests left out of a listing.
HIDDEN_WARNING_INTERVAL: Final = 60.0
_LOG = logging.getLogger(__name__)

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

    The policy is applied inside resolve(); a caller cannot skip it. The
    default, RoleApproverPolicy() with no map, refuses every request: an
    approver-side queue passes RoleApproverPolicy(roles_by_action=...), which
    decides the role each action needs. A requester-side queue never resolves,
    so it needs no policy. A denied attempt is audited and committed before its
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
        scrubber: Scrubber | None = None,
    ) -> None:
        self.database = database
        self._scrubber = scrubber if scrubber is not None else PatternScrubber()
        self._table = TableName.on(database, APPROVALS_TABLE, schema)
        self._audit_log = audit_log
        self._policy = policy if policy is not None else RoleApproverPolicy()
        self._clock = clock if clock is not None else _utc_now
        self._schema = self._table.schema or layout.DEFAULT_SCHEMA
        self._side: ApprovalSide | None = None
        self._last_hidden_warning = float("-inf")

    async def side(self, *, connection: Any = None) -> ApprovalSide:
        """Which side this queue's connection acts for, checking the setup first.

        Raises ConfigError if the Postgres schema or roles are wrong; see
        _postgres_schema.check_connection.
        """
        return await self._run(self._prepare, write=False, connection=connection)

    async def _run(
        self,
        work: Callable[[Session], Awaitable[ResultT]],
        *,
        write: bool,
        connection: Any,
    ) -> ResultT:
        """Run `work` in a transaction of its own, or in the host's on `connection`."""
        if connection is None:
            return await self.database.run(work, write=write)
        return await self.database.run_on(connection, work)

    async def _prepare(self, session: Session) -> ApprovalSide:
        """Check the connection once per queue, upgrading a SQLite file in place."""
        if self._side is None:
            if session.dialect is Dialect.SQLITE:
                await bring_table_up_to_date(session, APPROVALS_TABLE, ADDED_COLUMNS)
                self._side = ApprovalSide.BOTH
            else:
                side = ApprovalSide(await layout.check_connection(session, self._schema))
                # An installer that predates this release leaves columns out.
                await bring_table_up_to_date(
                    session, APPROVALS_TABLE, ADDED_COLUMNS, schema=self._table.schema
                )
                self._side = side
        return self._side

    async def _require_side(self, session: Session, operation: str, side: ApprovalSide) -> None:
        actual = await self._prepare(session)
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
        include_payload: bool = False,
        connection: Any = None,
    ) -> ApprovalRequest:
        """Queue a request that expires after ttl_seconds (at most TTL_SECONDS_MAX).

        The payload's hash is stored. The payload itself is stored only with
        include_payload=True: at most MAX_STORED_PAYLOAD_BYTES of canonical JSON,
        with the audit log's key, number and secret rules, else
        ApprovalPayloadRejectedError. Every read checks a stored payload against
        the hash before returning it (ApprovalIntegrityError otherwise), so the
        approver is shown what the hash binds. It is never copied into the audit
        log. `context`, the run asking, is stored on the request and its audit
        event. Only requested_by may consume the approval, unless `delegates` names
        other principals allowed to: an explicit choice, shown to the approver.

        With `connection`, a psycopg AsyncConnection already in a transaction, the
        request and its audit event are written in that transaction.
        """
        if not 0 < ttl_seconds <= TTL_SECONDS_MAX:
            raise ValueError(f"ttl_seconds must be 1 to {TTL_SECONDS_MAX}, got {ttl_seconds}")
        stored_payload = self._payload_to_store(payload) if include_payload else None
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
            payload=stored_payload,
        )

        async def insert(session: Session) -> _Outcome:
            await self._require_side(session, "submit requests", ApprovalSide.REQUESTER)
            await _ensure_table(session)
            await require_current_table(
                session, APPROVALS_TABLE, RUN_CONTEXT_COLUMN, schema=self._table.schema
            )
            await session.execute(
                f"INSERT INTO {self._table.sql} ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

        return await self._write(insert, connection=connection)

    def _payload_to_store(self, payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """The payload to keep with a request, or ApprovalPayloadRejectedError."""
        try:
            checked = check_payload(dict(payload), max_bytes=layout.MAX_STORED_PAYLOAD_BYTES)
        except ValueError as error:
            raise ApprovalPayloadRejectedError(f"The payload cannot be stored: {error}") from error
        if len(canonical_json(checked)) > layout.MAX_STORED_PAYLOAD_BYTES:
            raise ApprovalPayloadRejectedError(
                f"The payload cannot be stored: more than {layout.MAX_STORED_PAYLOAD_BYTES} bytes."
            )
        if _contains_nul(checked):
            # Postgres text cannot hold NUL; refuse it here rather than at the insert.
            raise ApprovalPayloadRejectedError("The payload cannot be stored: it contains NUL.")
        findings = self._scrubber.find_secrets({"payload": checked})
        if findings:
            located = ", ".join(f"{finding.rule} at {finding.path}" for finding in findings)
            raise ApprovalPayloadRejectedError(
                f"The payload cannot be stored: it contains {located}."
            )
        return checked

    async def get(self, request_id: UUID, *, connection: Any = None) -> ApprovalRequest:
        """Return the request; raises ApprovalNotFoundError if there is none, and
        ApprovalIntegrityError if its stored payload does not match its hash."""

        async def read(session: Session) -> ApprovalRequest | None:
            await self._prepare(session)
            return await _load(session, self._table, request_id, scrubber=self._scrubber)

        request = await self._run(read, write=False, connection=connection)
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
        connection: Any = None,
    ) -> Sequence[ApprovalRequest]:
        """Up to `limit` pending, unexpired requests this principal may resolve, oldest first.

        Requests are ordered by creation (created_at, then id). To read the next
        page, pass the last request's id as `after`; listing resumes right after
        it. An unknown `after` raises ApprovalNotFoundError.

        The policy decides each request. With the default RoleApproverPolicy the
        database also narrows by role and requester, which keeps a large queue
        cheap; with any other policy the queue is read page by page until `limit`
        requests pass or none are left, so a custom policy sees every candidate.

        A request whose stored payload does not match its hash is left out: the
        approver must not be shown it, and resolving it is refused and audited.
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
        scrubber = self._scrubber
        hidden: list[UUID] = []

        async def read_cursor(session: Session) -> ApprovalRequest | None:
            await self._prepare(session)
            # Only created_at and id are needed, so a row that fails the payload check
            # here does not stop pagination.
            return await _load(session, self._table, after) if after is not None else None

        resume_after: ApprovalRequest | None = None
        if after is not None:
            resume_after = await self._run(read_cursor, write=False, connection=connection)
            if resume_after is None:
                raise ApprovalNotFoundError(f"No approval request {after}.")

        async def read_pages(session: Session) -> bool:
            """Read pages until `limit` requests pass; return whether more pages remain."""
            nonlocal resume_after
            await self._prepare(session)
            while len(eligible) < limit:
                page = await _load_pending_page(
                    session,
                    self._table,
                    now=now,
                    after=resume_after,
                    narrowed_to=narrowed_to,
                    limit=page_size,
                    scrubber=scrubber,
                )
                hidden.extend(cursor.id for cursor, request in page if request is None)
                eligible.extend(
                    request
                    for _, request in page
                    if request is not None
                    and self._policy.evaluate(principal, request, now=now).allowed
                )
                if len(page) < page_size:
                    return False
                resume_after = page[-1][0]
                if session.dialect is Dialect.SQLITE:
                    return len(eligible) < limit
            return False

        # Postgres reads never block writers, so all pages share one transaction.
        # A SQLite read holds a lock that makes writers wait, so there each page is
        # its own short transaction.
        while await self._run(read_pages, write=False, connection=connection):
            pass
        self._warn_hidden(hidden)
        return eligible[:limit]

    def _warn_hidden(self, request_ids: list[UUID]) -> None:
        """Say, at most once a minute, that requests were left out of a listing.

        Hiding is silent to the approver by design; this is the signal for whoever
        runs the system that something wrote rows the checks refuse.
        """
        now = time.monotonic()
        if not request_ids or now - self._last_hidden_warning < HIDDEN_WARNING_INTERVAL:
            return
        self._last_hidden_warning = now
        _LOG.warning(
            "%d pending approval request(s) were left out of a listing because their stored "
            "payload failed the checks (first id %s). Someone with INSERT on "
            "the approvals table wrote them.",
            len(request_ids),
            request_ids[0],
        )

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
        connection: Any = None,
    ) -> ApprovalRequest:
        """Approve or reject a pending request, once.

        Raises NotAuthorizedToResolveError if the ApproverPolicy denies it,
        ApprovalAlreadyResolvedError if it is no longer pending,
        ApprovalExpiredError if it has expired, and ApprovalNotFoundError if it
        does not exist. Each of those is audited first, with `context` or, without
        one, the request's own.
        """

        async def decide(session: Session) -> _Outcome:
            await self._require_side(session, "decide requests", ApprovalSide.APPROVER)
            try:
                request = await _load(session, self._table, request_id, scrubber=self._scrubber)
            except ApprovalIntegrityError as error:
                # What is stored is not what the hash binds: nobody may approve it.
                return _denied(
                    error,
                    _missing_event(
                        "approval.resolve_denied",
                        principal.id,
                        request_id,
                        context,
                        reason="payload_integrity",
                    ),
                )
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
                },
                context={STORED_RECORD: True},
            )
            changed = await session.execute_count(
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

        return await self._write(decide, connection=connection, stored_context=True)

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
        connection: Any = None,
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

        async def use(session: Session) -> _Outcome:
            await self._require_side(session, "consume approvals", ApprovalSide.REQUESTER)
            request = await _load(session, self._table, request_id)
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
                {**request.model_dump(), "status": ApprovalStatus.CONSUMED, "consumed_at": now},
                context={STORED_RECORD: True},
            )
            changed = await session.execute_count(
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

        return await self._write(use, connection=connection, stored_context=True)

    async def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
        connection: Any = None,
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

        async def withdraw(session: Session) -> _Outcome:
            await self._require_side(session, "cancel requests", ApprovalSide.REQUESTER)
            request = await _load(session, self._table, request_id)
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
            if not await _close(session, self._table, request, ApprovalStatus.CANCELLED, now):
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

        return await self._write(withdraw, connection=connection, stored_context=True)

    async def expire_due(
        self,
        *,
        principal: Principal,
        now: datetime | None = None,
        limit: int = 500,
        connection: Any = None,
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

        async def sweep(session: Session) -> _Outcome:
            await self._prepare(session)
            if not await _table_exists(session, self._table):
                return _Outcome()
            due = await _load_due(session, self._table, moment, limit)
            events = []
            for request in due:
                if await _close(session, self._table, request, ApprovalStatus.EXPIRED, moment):
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
            outcome = await self._write_outcome(sweep, connection=connection, stored_context=True)
            total += outcome.expired
            if not outcome.batch_full:
                return total

    async def _write(
        self,
        work: Callable[[Session], Awaitable[_Outcome]],
        *,
        connection: Any = None,
        stored_context: bool = False,
    ) -> ApprovalRequest:
        """Run `work` as _write_outcome does, then return its request or raise its error."""
        outcome = await self._write_outcome(
            work, connection=connection, stored_context=stored_context
        )
        if outcome.error is not None:
            raise outcome.error
        if outcome.request is None:
            raise AssertionError("an approval transaction must return a request or an error")
        return outcome.request

    async def _write_outcome(
        self,
        work: Callable[[Session], Awaitable[_Outcome]],
        *,
        connection: Any = None,
        stored_context: bool = False,
    ) -> _Outcome:
        """Run `work` in a write transaction and audit its events.

        When the audit log is on the same database the events are written in the
        same transaction, so the change and its record commit together or not at
        all. An audit log elsewhere is written right after the commit; that is best
        effort, since a failure then cannot undo the change.

        On a host's `connection` the same holds for what succeeded: the host's
        commit or rollback decides it. A refusal is different. It changed nothing,
        and the host will most likely roll back when the error reaches it, which
        would erase the record of the refusal; so it is written on a connection of
        the audit log's own pool, committed at once. If that cannot be done
        promptly (no pooled connection within DENIAL_ACQUIRE_TIMEOUT, the append lock
        still held after DENIAL_LOCK_TIMEOUT by the host's own earlier audit write, the
        database fails) the failure is logged and the record goes into the host's
        transaction instead, where it lasts only if the host commits.

        With `connection` the log must share this queue's database, or ConfigError.
        """
        audit_log = self._audit_log
        checked_log = audit_log if isinstance(audit_log, SQLAuditLog) else None
        shares_database = checked_log is not None and checked_log.database.same_database(
            self.database
        )
        if connection is not None and not shares_database:
            # Events appended through another log commit at once, before the host does,
            # so a host rollback would leave records of things that never happened.
            raise ConfigError(
                "connection= needs an audit log that shares this queue's database: pass the "
                "same Database object to the SQLAuditLog and the queue, or one opened from "
                "the same connection settings."
            )

        async def in_transaction(session: Session) -> _Outcome:
            outcome = await work(session)
            # Checked before the commit, so an event the log would refuse stops the change.
            if checked_log is not None:
                # Events about a stored request may carry its context, which can fail
                # today's rules; a new request's own context must pass them.
                outcome.events = [
                    _checked_or_without_context(checked_log, event)
                    if stored_context
                    else checked_log.checked_event(event)
                    for event in outcome.events
                ]
            keep_apart = connection is not None and outcome.error is not None
            if shares_database and checked_log is not None and not keep_apart:
                for event in outcome.events:
                    await checked_log.append_in(session, event)
            return outcome

        outcome = await self._run(in_transaction, write=True, connection=connection)
        if not shares_database:
            for event in outcome.events:
                await audit_log.append(event)
        elif connection is not None and outcome.error is not None and checked_log is not None:
            await self._record_refusal(checked_log, outcome, connection)
        return outcome

    async def _record_refusal(self, log: SQLAuditLog, outcome: _Outcome, connection: Any) -> None:
        """Write a refusal's events apart from the host's transaction, or fall back to it.

        If neither write succeeds the refusal is still raised to the caller, with a note
        on it saying it went unaudited, and the details are logged: the host's commit
        will not report it.
        """
        events = outcome.events
        described = _describe(events)

        async def apart(session: Session) -> list[Any]:
            if session.dialect is Dialect.POSTGRES:
                # Waits only briefly for the append lock: the host's own transaction
                # may hold it already, and it is waiting for us.
                await session.execute(f"SET LOCAL lock_timeout = '{DENIAL_LOCK_TIMEOUT}'")
            return await log.append_many_in(session, events)

        async def inside(session: Session) -> list[Any]:
            if session.dialect is not Dialect.POSTGRES:
                return await log.append_many_in(session, events)
            # The same bound, for this savepoint only: put back before the host goes on.
            previous = (await session.execute("SELECT current_setting('lock_timeout')"))[0][0]
            await session.execute(f"SET LOCAL lock_timeout = '{DENIAL_LOCK_TIMEOUT}'")
            records = await log.append_many_in(session, events)
            await session.execute("SELECT set_config('lock_timeout', ?, true)", (previous,))
            return records

        failures: tuple[type[Exception], ...] = (*driver_errors(), TimeoutError)
        try:
            await log.database.run(apart, write=True, acquire_timeout=DENIAL_ACQUIRE_TIMEOUT)
        except failures as failure:
            # Not silent: the refusal now lasts only if the host commits.
            _LOG.warning(
                "A refusal could not be audited apart from the host's transaction (%s: %s); "
                "it is written in the host's transaction and is lost if that rolls back. [%s]",
                type(failure).__name__,
                failure,
                described,
            )
            try:
                await self.database.run_on(connection, inside)
            except failures as second:
                _LOG.error(
                    "A refusal could not be audited at all (%s: %s). [%s]",
                    type(second).__name__,
                    second,
                    described,
                )
                if outcome.error is not None:
                    outcome.error.add_note(
                        "This refusal was not audited: neither the separate write nor the "
                        "write in the host's transaction succeeded."
                    )


def _checked_or_without_context(log: SQLAuditLog, event: AuditEvent) -> AuditEvent:
    """The event as the log accepts it. A run context stored with a request may fail the
    rules in force now (it passed the ones in force when it was written, or a requester
    wrote it with plain SQL): such an event is written without it, and says so, rather
    than blocking the decision, the refusal or the expiry sweep it describes."""
    try:
        return log.checked_event(event)
    except AuditPayloadRejectedError:
        if event.context is None:
            raise
        stripped = event.model_copy(
            update={"context": None, "payload": {**event.payload, "run_context_dropped": "true"}}
        )
        return log.checked_event(stripped)


def _contains_nul(value: JsonValue) -> bool:
    """True when any key or string in `value` holds a NUL character."""
    stack: list[JsonValue] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            if any("\x00" in key for key in item):
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _describe(events: list[AuditEvent]) -> str:
    """What events say happened, for a log line: identifiers and reasons, never payloads."""
    return "; ".join(
        f"{event.action} subject={event.subject_id} actor={event.actor_id} "
        f"reason={event.payload.get('reason')}"
        for event in events
    )


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


async def _close(
    session: Session,
    table: TableName,
    request: ApprovalRequest,
    status: ApprovalStatus,
    now: datetime,
) -> bool:
    """Move a pending request to `status` (expired or cancelled); False if it moved meanwhile."""
    changed = await session.execute_count(
        f"UPDATE {table.sql} SET status = ?, closed_at = ? WHERE id = ? AND status = ?",
        (status.value, canonical_timestamp(now), str(request.id), ApprovalStatus.PENDING.value),
    )
    return changed == 1


# The database's clock as a canonical timestamp, so the sweep picks only what the
# guard agrees has expired, even when the application's clock runs ahead.
_POSTGRES_STATEMENT_NOW_TEXT = (
    "to_char(statement_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
)


async def _load_due(
    session: Session, table: TableName, now: datetime, limit: int
) -> list[ApprovalRequest]:
    """Up to `limit` pending requests whose lifetime ended by `now`, oldest expiry first."""
    database_clock = (
        f" AND expires_at <= {_POSTGRES_STATEMENT_NOW_TEXT}"
        if session.dialect is Dialect.POSTGRES
        else ""
    )
    rows = await session.execute(
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
    payload: dict[str, JsonValue] = {"approval_action": request.action, **details}
    try:
        return AuditEvent(
            action=action,
            actor_id=actor_id,
            subject_id=str(request.id),
            payload=payload,
            context=context,
        )
    except ValidationError:
        # A context stored with the request that the rules in force now refuse (or that a
        # requester wrote with plain SQL): the event is written without it, and says so.
        if context is None:
            raise
        return AuditEvent(
            action=action,
            actor_id=actor_id,
            subject_id=str(request.id),
            payload={**payload, "run_context_dropped": "true"},
            context=None,
        )


def _missing_event(
    action: str,
    actor_id: str | None,
    request_id: UUID,
    context: RunContext | None,
    *,
    reason: str = "not_found",
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_id=actor_id or "unknown",
        subject_id=str(request_id),
        payload={"reason": reason},
        context=context,
    )


async def _ensure_table(session: Session) -> None:
    # On Postgres the owner role installs the table; the app role cannot create it.
    if session.dialect is Dialect.SQLITE:
        await session.execute(_TABLE_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        await session.execute(
            _PENDING_INDEX_DDL.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
        )


async def _table_exists(session: Session, table: TableName) -> bool:
    """Whether the table exists; ConfigError if it is a 0.1.0a1 table without run_context."""
    # Reads before the first submit see "no table", which means "no requests".
    if session.dialect is Dialect.SQLITE:
        exists = bool(
            await session.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (APPROVALS_TABLE,),
            )
        )
    else:
        exists = bool(
            (await session.execute("SELECT to_regclass(?) IS NOT NULL", (table.sql,)))[0][0]
        )
    if exists:
        await require_current_table(
            session, APPROVALS_TABLE, RUN_CONTEXT_COLUMN, schema=table.schema
        )
    return exists


async def _load(
    session: Session, table: TableName, request_id: UUID, *, scrubber: Scrubber | None = None
) -> ApprovalRequest | None:
    """The request. With a `scrubber`, a stored payload is checked and returned too.

    The check is _with_payload's: ApprovalIntegrityError if it fails. Without one
    the request comes back with payload None, however it was stored: cancelling and
    consuming show the payload to nobody, and the requester must be able to
    withdraw a request whatever is stored with it.
    """
    if not await _table_exists(session, table):
        return None
    rows = await session.execute(
        f"SELECT {_COLUMNS} FROM {table.sql} WHERE id = ?", (str(request_id),)
    )
    if not rows:
        return None
    request, payload_text = _parse_row(rows[0])
    return request if scrubber is None else _with_payload(request, payload_text, scrubber)


def _with_payload(
    request: ApprovalRequest, payload_text: str | None, scrubber: Scrubber
) -> ApprovalRequest:
    """The request carrying its stored payload, after every check a submit applies.

    The stored text must parse, pass the audit log's rules for a payload (keys,
    numbers, size), hold no secret, and hash to payload_sha256. Whoever wrote the
    row, the requester with plain SQL included, anything else is an integrity error:
    the approver is shown nothing the hash does not bind.
    """
    if payload_text is None:
        return request
    try:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            raise ValueError("a stored payload is a JSON object")
        # Cheapest checks first: whoever can insert rows can make a reader do this
        # for every one of them, on every listing.
        if exceeds_depth(payload):
            raise ValueError("a stored payload nests too deeply")
        if approval_payload_hash(request.action, payload) != request.payload_sha256:
            raise ValueError("a stored payload does not match payload_sha256")
        check_payload(payload, max_bytes=layout.MAX_STORED_PAYLOAD_BYTES)
        if scrubber.find_secrets({"payload": payload}):
            raise ValueError("a stored payload holds a secret")
    except (ValueError, TypeError, RecursionError) as error:
        raise ApprovalIntegrityError(
            f"The payload stored with request {request.id} is not one its payload_sha256 binds."
        ) from error
    return request.model_copy(update={"payload": payload})


async def _load_pending_page(
    session: Session,
    table: TableName,
    *,
    now: datetime,
    after: ApprovalRequest | None,
    narrowed_to: Principal | None,
    limit: int,
    scrubber: Scrubber,
) -> list[tuple[ApprovalRequest, ApprovalRequest | None]]:
    """One page of pending, unexpired requests after `after`, in (created_at, id) order.

    Each is a pair: the request as the page cursor needs it, and the request with its
    checked payload, or None when the stored payload fails the check and must not be
    shown.

    With `narrowed_to`, only requests that principal could resolve under the
    default policy: a role it holds, and not its own.
    """
    if not await _table_exists(session, table):
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
    rows = await session.execute(
        f"SELECT {_COLUMNS} FROM {table.sql} WHERE {' AND '.join(conditions)} "
        "ORDER BY created_at, id LIMIT ?",
        (*parameters, limit),
    )
    # Checking a page is CPU work that the requester role can multiply, so it runs off
    # the event loop.
    return await asyncio.to_thread(_verify_rows, rows, scrubber)


def _verify_rows(
    rows: list[tuple[Any, ...]], scrubber: Scrubber
) -> list[tuple[ApprovalRequest, ApprovalRequest | None]]:
    page: list[tuple[ApprovalRequest, ApprovalRequest | None]] = []
    for row in rows:
        request, payload_text = _parse_row(row)
        try:
            page.append((request, _with_payload(request, payload_text, scrubber)))
        except ApprovalIntegrityError:
            page.append((request, None))
    return page


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
        (canonical_json(request.payload).decode("utf-8") if request.payload is not None else None),
    )


def _parse_row(row: tuple[Any, ...]) -> tuple[ApprovalRequest, str | None]:
    """The request without its payload, and the stored payload text, unparsed.

    The run context is read back structurally only: it was checked when written, and a
    rule added later must not make a stored request unreadable. Whatever writes
    audit events from it copes with a context the current rules refuse
    (_write_outcome drops it from the event).
    """
    # NULL columns are dropped so the model's defaults apply.
    names = [name.strip() for name in _COLUMNS.split(",")]
    fields = {name: value for name, value in zip(names, row, strict=True) if value is not None}
    payload_text = fields.pop(PAYLOAD_COLUMN, None)
    if RUN_CONTEXT_COLUMN in fields:
        fields[RUN_CONTEXT_COLUMN] = json.loads(fields[RUN_CONTEXT_COLUMN])
    fields[DELEGATES_COLUMN] = json.loads(fields.get(DELEGATES_COLUMN, "[]"))
    return ApprovalRequest.model_validate(fields, context={STORED_RECORD: True}), payload_text


def _request_from_row(row: tuple[Any, ...]) -> ApprovalRequest:
    """The request with payload None: for reads that show no payload to anyone."""
    return _parse_row(row)[0]


def _utc_now() -> datetime:
    return datetime.now(UTC)
