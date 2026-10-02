"""Running a suite against a target and scoring the results."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, JsonValue

from aox_agent_core.config import Tier
from aox_agent_core.evals.types import (
    CaseResult,
    EvalCase,
    EvalSuite,
    Score,
    Scorecard,
    TargetOutput,
)
from aox_agent_core.models.client import AgentClient
from aox_agent_core.replay.scrub import PatternScrubber

# The system under test: takes one case and returns its output and cost.
EvalTarget = Callable[[EvalCase], Awaitable[TargetOutput]]

MAX_ERROR_LENGTH = 200


class Scorer(Protocol):
    """Judges one case's output. name labels its scores in the scorecard."""

    @property
    def name(self) -> str: ...

    def score(self, case: EvalCase, output: JsonValue) -> Score: ...


class EvalRunner:
    """Runs every case through the target, at most `concurrency` at a time.

    Latency is measured around each target call. A target that raises is
    recorded as a failed case with its error type and message; the run
    continues. A scorer that raises is not caught and fails the whole run. A target
    built on an AgentClient in replay mode (the client's default) costs nothing.
    """

    def __init__(self, scorers: Sequence[Scorer], *, concurrency: int = 4) -> None:
        if not scorers:
            raise ValueError("an eval run needs at least one scorer")
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        self._scorers = tuple(scorers)
        self._concurrency = concurrency

    async def run(self, suite: EvalSuite, target: EvalTarget) -> Scorecard:
        """Run the suite and return its scorecard, results in the suite's case order."""
        started_at = datetime.now(UTC)
        slots = asyncio.Semaphore(self._concurrency)

        async def run_case(case: EvalCase) -> CaseResult:
            async with slots:
                return await self._run_case(case, target)

        results = await asyncio.gather(*(run_case(case) for case in suite.cases))
        return _scorecard(suite.name, started_at, datetime.now(UTC), tuple(results))

    async def _run_case(self, case: EvalCase, target: EvalTarget) -> CaseResult:
        started = time.perf_counter()
        try:
            produced = await target(case)
        except Exception as error:
            return CaseResult(
                case_id=case.id,
                latency_ms=(time.perf_counter() - started) * 1000,
                cost_usd=Decimal(0),
                error=_scrubbed_error(error),
            )
        latency_ms = (time.perf_counter() - started) * 1000
        scores = tuple(scorer.score(case, produced.output) for scorer in self._scorers)
        return CaseResult(
            case_id=case.id,
            output=produced.output,
            scores=scores,
            latency_ms=latency_ms,
            cost_usd=produced.cost_usd,
        )


def model_call_target(
    client: AgentClient,
    *,
    output: type[BaseModel] | None = None,
    tier: Tier | None = None,
    task: str | None = None,
    system: str | None = None,
) -> EvalTarget:
    """A target that sends each case's input (a string) as the prompt.

    Structured output is returned as its JSON form, so FieldMatch can compare it,
    and the call's cost is carried into the scorecard. A non-string input raises
    TypeError, which the runner records as that case's failure.
    """

    async def call(case: EvalCase) -> TargetOutput:
        if not isinstance(case.input, str):
            raise TypeError(f"case {case.id} input must be a string prompt")
        result = await client.call(case.input, output=output, tier=tier, task=task, system=system)
        produced: JsonValue = (
            result.output.model_dump(mode="json")
            if isinstance(result.output, BaseModel)
            else result.output
        )
        return TargetOutput(output=produced, cost_usd=result.cost_usd)

    return call


def _scorecard(
    suite: str, started_at: datetime, finished_at: datetime, results: tuple[CaseResult, ...]
) -> Scorecard:
    # The suite has at least one case, so the divisions and percentiles below are safe.
    passed = [
        result
        for result in results
        if result.error is None and result.scores and all(score.passed for score in result.scores)
    ]
    latencies = sorted(result.latency_ms for result in results)
    total_cost = sum((result.cost_usd for result in results), Decimal(0))
    return Scorecard(
        suite=suite,
        started_at=started_at,
        finished_at=finished_at,
        results=results,
        accuracy=len(passed) / len(results),
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        cost_total_usd=total_cost,
        cost_per_case_usd=total_cost / len(results),
    )


def _percentile(sorted_values: list[float], percent: int) -> float:
    """Nearest-rank percentile: the smallest value with at least `percent`% at or below it."""
    rank = max(1, math.ceil(percent / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _scrubbed_error(error: Exception) -> str:
    """The error's type and message, with anything secret-shaped redacted, then shortened.

    A target's error can quote a request or a credential, and scorecards are shared.
    """
    redacted = PatternScrubber().redact(f"{type(error).__name__}: {error}")
    return str(redacted)[:MAX_ERROR_LENGTH]
