"""Blocking facades over the async audit log and approval queue, for scripts.

The library is async-first. A script that has no event loop of its own wraps a log
or a queue here and calls it like ordinary functions:

    log = SyncAuditLog(SQLAuditLog(open_database(url)))
    record = log.append(event)
    log.close()

Each wrapper runs its calls on one background thread with its own event loop, so
a connection pool opened by the first call is used by every later one, and close()
shuts it down. Do not call a facade from inside a running event loop; await the
async object instead. A host transaction (`connection=`) belongs to the loop that
owns the connection, so the facades do not accept one.
"""

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TypeVar
from uuid import UUID

from pydantic import JsonValue

from aox_agent_core.approvals.queue import ApprovalQueue
from aox_agent_core.approvals.types import ApprovalRequest, Decision, Principal
from aox_agent_core.audit.log import AuditLog
from aox_agent_core.audit.types import AuditEvent, AuditHead, AuditRecord
from aox_agent_core.context import RunContext
from aox_agent_core.errors import EventLoopRunningError

ResultT = TypeVar("ResultT")


class _Runner:
    """One event loop on one daemon thread, started on first use."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def run(self, work: Awaitable[ResultT]) -> ResultT:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if inspect.iscoroutine(work):
                work.close()  # never started; do not leave it to warn
            raise EventLoopRunningError(
                "A blocking facade was called from inside a running event loop; "
                "await the async log or queue instead."
            )
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(_as_coroutine(work), loop).result()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="agent-core-sync", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def close(self, *, then: Callable[[], Awaitable[None]] | None = None) -> None:
        """Run `then()` (to release a pool, say), then stop the loop and its thread.

        Nothing runs if the loop never started: nothing was opened either.
        """
        with self._start_lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        try:
            if then is not None:
                asyncio.run_coroutine_threadsafe(_as_coroutine(then()), loop).result()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()


async def _as_coroutine(work: Awaitable[ResultT]) -> ResultT:
    return await work


async def _close_database(owner: object) -> None:
    database = getattr(owner, "database", None)
    aclose = getattr(database, "aclose", None)
    if aclose is not None:
        await aclose()


class SyncAuditLog:
    """An AuditLog called without `await`. Same names, same results."""

    def __init__(self, log: AuditLog) -> None:
        self._log = log
        self._runner = _Runner()

    def append(self, event: AuditEvent) -> AuditRecord:
        return self._runner.run(self._log.append(event))

    def append_many(self, events: Sequence[AuditEvent]) -> list[AuditRecord]:
        return self._runner.run(self._log.append_many(events))

    def head(self) -> AuditHead:
        return self._runner.run(self._log.head())

    def verify(self, *, expected_head: AuditHead | None = None) -> AuditHead:
        return self._runner.run(self._log.verify(expected_head=expected_head))

    def iter_records(self, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        records = self._log.iter_records(after_seq=after_seq)

        async def next_record() -> AuditRecord | None:
            return await anext(records, None)

        while (record := self._runner.run(next_record())) is not None:
            yield record

    def close(self) -> None:
        """Close the log's database, if it has one, and stop the background loop."""
        self._runner.close(then=lambda: _close_database(self._log))

    def __enter__(self) -> "SyncAuditLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SyncApprovalQueue:
    """An ApprovalQueue called without `await`. Same names, same results."""

    def __init__(self, queue: ApprovalQueue) -> None:
        self._queue = queue
        self._runner = _Runner()

    def submit(
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
    ) -> ApprovalRequest:
        return self._runner.run(
            self._queue.submit(
                action=action,
                summary=summary,
                payload=payload,
                requested_by=requested_by,
                required_role=required_role,
                ttl_seconds=ttl_seconds,
                delegates=delegates,
                context=context,
                include_payload=include_payload,
            )
        )

    def get(self, request_id: UUID) -> ApprovalRequest:
        return self._runner.run(self._queue.get(request_id))

    def list_pending(
        self, principal: Principal, *, limit: int = 100, after: UUID | None = None
    ) -> Sequence[ApprovalRequest]:
        return self._runner.run(self._queue.list_pending(principal, limit=limit, after=after))

    def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        return self._runner.run(
            self._queue.resolve(
                request_id,
                decision=decision,
                principal=principal,
                reason=reason,
                context=context,
            )
        )

    def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        return self._runner.run(
            self._queue.consume(
                request_id, action=action, payload=payload, principal=principal, context=context
            )
        )

    def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        return self._runner.run(
            self._queue.cancel(request_id, principal=principal, reason=reason, context=context)
        )

    def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = 500
    ) -> int:
        return self._runner.run(self._queue.expire_due(principal=principal, now=now, limit=limit))

    def purge_payloads(
        self, *, principal: Principal, older_than: timedelta, limit: int = 500
    ) -> int:
        return self._runner.run(
            self._queue.purge_payloads(principal=principal, older_than=older_than, limit=limit)
        )

    def close(self) -> None:
        """Close the queue's database, if it has one, and stop the background loop."""
        self._runner.close(then=lambda: _close_database(self._queue))

    def __enter__(self) -> "SyncApprovalQueue":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["SyncApprovalQueue", "SyncAuditLog"]
