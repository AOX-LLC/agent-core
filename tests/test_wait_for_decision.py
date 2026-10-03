"""wait_for_decision: returns on any decision, backs off with jitter, times out, holds nothing."""

import asyncio
import pickle
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from aox_agent_core.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
    wait_for_decision,
)
from aox_agent_core.approvals import wait as wait_module
from aox_agent_core.errors import ApprovalNotFoundError, ApprovalWaitTimeoutError
from aox_agent_core.sync import SyncApprovalQueue
from databases import ControlDatabase, SplitQueue, split_queue, sqlite_database
from test_approvals import APPROVER, REQUESTER

ACTION = "crm.update_contact"


class FakeQueue:
    """get() answers pending until `decide_after` reads, then the decided request."""

    def __init__(self, request: ApprovalRequest, *, decide_after: int | None) -> None:
        self.request = request
        self.decide_after = decide_after
        self.reads = 0

    async def get(self, request_id: UUID) -> ApprovalRequest:
        self.reads += 1
        if request_id != self.request.id:
            raise ApprovalNotFoundError(f"No approval request {request_id}.")
        if self.decide_after is not None and self.reads > self.decide_after:
            return self.request.model_copy(update={"status": ApprovalStatus.REJECTED})
        return self.request


def pending_stub() -> ApprovalRequest:
    """A pending request with only the fields the wait reads, for a fake queue."""
    return ApprovalRequest.model_construct(id=uuid4(), status=ApprovalStatus.PENDING)  # type: ignore[call-arg]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every pause and advance a fake monotonic clock by it instead of sleeping."""
    recorded: list[float] = []
    now = [1_000.0]

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(wait_module, "_sleep", fake_sleep)
    monkeypatch.setattr(wait_module, "_monotonic", lambda: now[0])
    monkeypatch.setattr(wait_module, "_uniform", lambda low, high: 1.0)
    return recorded


async def a_pending_request(queue: SplitQueue) -> ApprovalRequest:
    return await queue.submit(
        action=ACTION,
        summary="s",
        payload={"a": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=3_600,
    )


async def test_it_returns_at_once_for_a_request_already_decided(tmp_path: Path) -> None:
    queue = split_queue(sqlite_database(tmp_path))
    request = await a_pending_request(queue)
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    decided = await wait_for_decision(queue.requester, request.id, timeout=timedelta(seconds=5))

    assert decided.status is ApprovalStatus.APPROVED


async def test_it_returns_what_the_decider_decided_while_it_waited(tmp_path: Path) -> None:
    queue = split_queue(sqlite_database(tmp_path))
    request = await a_pending_request(queue)

    async def decide_soon() -> None:
        await asyncio.sleep(0.15)
        await queue.resolve(request.id, decision=Decision.REJECT, principal=APPROVER)

    decider = asyncio.create_task(decide_soon())
    decided = await wait_for_decision(
        queue.requester,
        request.id,
        timeout=timedelta(seconds=10),
        poll_interval=timedelta(milliseconds=20),
    )
    await decider

    assert decided.status is ApprovalStatus.REJECTED


async def test_a_request_that_lapses_ends_the_wait_as_expired(tmp_path: Path) -> None:
    queue = split_queue(sqlite_database(tmp_path))
    request = await queue.submit(
        action=ACTION,
        summary="s",
        payload={"a": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=1,
    )

    ended = await wait_for_decision(
        queue.requester,
        request.id,
        timeout=timedelta(seconds=10),
        poll_interval=timedelta(milliseconds=100),
    )

    assert ended.status is ApprovalStatus.EXPIRED


async def test_the_pause_doubles_with_jitter_up_to_the_ceiling(sleeps: list[float]) -> None:
    request = pending_stub()
    queue = FakeQueue(request, decide_after=9)

    await wait_for_decision(
        queue,  # type: ignore[arg-type]
        request.id,
        timeout=timedelta(hours=72),
        poll_interval=timedelta(seconds=1),
        max_poll_interval=timedelta(seconds=30),
    )

    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30, 30]


async def test_jitter_spreads_each_pause_within_its_range(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    request = pending_stub()
    queue = FakeQueue(request, decide_after=3)
    monkeypatch.undo()  # real jitter, but still no sleeping
    spent: list[float] = []

    async def no_sleep(seconds: float) -> None:
        spent.append(seconds)

    monkeypatch.setattr(wait_module, "_sleep", no_sleep)
    await wait_for_decision(
        queue,  # type: ignore[arg-type]
        request.id,
        timeout=timedelta(hours=1),
        poll_interval=timedelta(seconds=4),
        max_poll_interval=timedelta(seconds=30),
    )

    for pause, base in zip(spent, (4, 8, 16), strict=True):
        assert base * 0.75 <= pause <= base * 1.25


async def test_a_long_wait_does_not_poll_every_second(sleeps: list[float]) -> None:
    request = pending_stub()
    queue = FakeQueue(request, decide_after=None)

    with pytest.raises(ApprovalWaitTimeoutError):
        await wait_for_decision(
            queue,  # type: ignore[arg-type]
            request.id,
            timeout=timedelta(hours=72),
        )

    # 72 hours at the 30 second ceiling is under 9,000 reads, not 259,200.
    assert queue.reads < 9_000
    assert max(sleeps) <= 30


async def test_the_timeout_raises_with_the_still_pending_request(sleeps: list[float]) -> None:
    request = pending_stub()
    queue = FakeQueue(request, decide_after=None)

    with pytest.raises(ApprovalWaitTimeoutError, match="still pending") as caught:
        await wait_for_decision(
            queue,  # type: ignore[arg-type]
            request.id,
            timeout=timedelta(seconds=10),
            poll_interval=timedelta(seconds=3),
        )

    assert caught.value.request_id == request.id
    assert caught.value.last is request
    assert sum(sleeps) == pytest.approx(10)  # never sleeps past the deadline
    copied: Any = pickle.loads(pickle.dumps(caught.value))  # noqa: S301
    assert copied.request_id == request.id


@pytest.mark.parametrize(
    "arguments",
    [
        {"timeout": timedelta(0)},
        {"timeout": timedelta(seconds=-1)},
        {"timeout": timedelta(seconds=1), "poll_interval": timedelta(0)},
        {
            "timeout": timedelta(seconds=1),
            "poll_interval": timedelta(seconds=5),
            "max_poll_interval": timedelta(seconds=1),
        },
    ],
)
async def test_bad_durations_are_refused(arguments: dict[str, timedelta]) -> None:
    request = pending_stub()

    with pytest.raises(ValueError, match="must be"):
        await wait_for_decision(FakeQueue(request, decide_after=0), request.id, **arguments)  # type: ignore[arg-type]


async def test_an_unknown_request_raises_not_found(tmp_path: Path) -> None:
    queue = split_queue(sqlite_database(tmp_path))
    await a_pending_request(queue)

    with pytest.raises(ApprovalNotFoundError):
        await wait_for_decision(queue.requester, uuid4(), timeout=timedelta(seconds=1))


async def test_cancelling_the_wait_stops_it_and_leaves_the_request_alone(tmp_path: Path) -> None:
    queue = split_queue(sqlite_database(tmp_path))
    request = await a_pending_request(queue)
    waiting = asyncio.create_task(
        wait_for_decision(
            queue.requester,
            request.id,
            timeout=timedelta(hours=1),
            poll_interval=timedelta(milliseconds=10),
        )
    )
    await asyncio.sleep(0.05)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert (await queue.get(request.id)).status is ApprovalStatus.PENDING


async def test_the_wait_works_on_both_backends_through_the_requester_side(
    control_database: ControlDatabase,
) -> None:
    queue = split_queue(control_database)
    request = await a_pending_request(queue)
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)

    decided = await wait_for_decision(queue.requester, request.id, timeout=timedelta(seconds=5))

    assert decided.status is ApprovalStatus.APPROVED
    assert decided.resolved_by == APPROVER.id


def test_the_sync_facade_waits_with_the_same_arguments(tmp_path: Path) -> None:
    database = sqlite_database(tmp_path)
    queue = split_queue(database)
    sync = SyncApprovalQueue(queue.requester)
    other = Principal(id="user-23", kind=PrincipalKind.HUMAN, roles=APPROVER.roles)
    request = sync.submit(
        action=ACTION,
        summary="s",
        payload={"a": 1},
        requested_by=REQUESTER,
        required_role="ops.approver",
        ttl_seconds=600,
    )
    try:
        with pytest.raises(ApprovalWaitTimeoutError):
            sync.wait_for_decision(
                request.id,
                timeout=timedelta(milliseconds=200),
                poll_interval=timedelta(milliseconds=20),
                max_poll_interval=timedelta(milliseconds=50),
            )
        assert other.id == "user-23"
    finally:
        sync.close()
