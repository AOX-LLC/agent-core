"""Waiting for a person to decide a request: polling with backoff, over any queue."""

import asyncio
import random
import time
from datetime import timedelta
from typing import Final
from uuid import UUID

from aox_agent_core.approvals.queue import ApprovalQueue
from aox_agent_core.approvals.types import ApprovalRequest, ApprovalStatus
from aox_agent_core.errors import ApprovalWaitTimeoutError

DEFAULT_POLL_INTERVAL: Final = timedelta(seconds=1)
DEFAULT_MAX_POLL_INTERVAL: Final = timedelta(seconds=30)
# Each wait is the current interval times a factor in this range, so many waiters that
# started together do not read the queue in step.
JITTER_RANGE: Final = (0.75, 1.25)

# Looked up here so a test can replace them without touching asyncio itself.
_sleep = asyncio.sleep
_monotonic = time.monotonic
_uniform = random.uniform


async def wait_for_decision(
    queue: ApprovalQueue,
    request_id: UUID,
    *,
    timeout: timedelta,  # noqa: ASYNC109 - a plain duration; the wait is a polling loop
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
    max_poll_interval: timedelta = DEFAULT_MAX_POLL_INTERVAL,
) -> ApprovalRequest:
    """Wait until the request is no longer pending, and return it.

    Reads the request at once, then polls with get(): the wait starts at `poll_interval`
    and roughly doubles after each read, with jitter, up to `max_poll_interval`, so a
    wait of days does not read every second. Returns as soon as the status is anything
    but PENDING: APPROVED, REJECTED, CANCELLED or EXPIRED (a request past its lifetime
    reads as expired, so the wait ends at expiry), or CONSUMED. The caller decides what
    each means; an approved request still has to be consumed. Raises
    ApprovalWaitTimeoutError if `timeout` passes first, and whatever get() raises
    (ApprovalNotFoundError, ApprovalIntegrityError).

    It works over any ApprovalQueue, uses no connection of the caller's, and holds
    nothing between reads: each is its own short transaction, so it is safe behind a
    transaction-mode pooler and for a wait of days. No sleep is longer than the time
    left. Cancelling the task cancels the wait and nothing else.
    """
    if timeout <= timedelta(0):
        raise ValueError("timeout must be positive")
    if poll_interval <= timedelta(0):
        raise ValueError("poll_interval must be positive")
    if max_poll_interval < poll_interval:
        raise ValueError("max_poll_interval must be at least poll_interval")

    deadline = _monotonic() + timeout.total_seconds()
    interval = poll_interval.total_seconds()
    ceiling = max_poll_interval.total_seconds()
    while True:
        request = await queue.get(request_id)
        if request.status is not ApprovalStatus.PENDING:
            return request
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise ApprovalWaitTimeoutError(
                f"Request {request_id} is still pending after {timeout}.",
                request_id=request_id,
                last=request,
            )
        jitter = _uniform(*JITTER_RANGE)
        pause = min(interval * jitter, ceiling, remaining)
        await _sleep(pause)
        interval = min(interval * 2, ceiling)
