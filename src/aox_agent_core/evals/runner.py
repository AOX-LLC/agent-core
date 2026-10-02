"""Running a suite against a target and scoring the results."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from pydantic import JsonValue

from aox_agent_core.evals.types import EvalCase, EvalSuite, Score, Scorecard, TargetOutput

EvalTarget = Callable[[EvalCase], Awaitable[TargetOutput]]


class Scorer(Protocol):
    """Judges one case's output. name labels its scores in the scorecard."""

    @property
    def name(self) -> str: ...

    def score(self, case: EvalCase, output: JsonValue) -> Score: ...


class EvalRunner:
    """Runs every case through the target, at most `concurrency` at a time.

    Latency is measured around each target call. A target that raises is
    recorded as a failed case with its error message; the run continues. In CI
    the target runs in replay mode, so a run costs nothing.
    """

    def __init__(self, scorers: Sequence[Scorer], *, concurrency: int = 4) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        self._scorers = tuple(scorers)
        self._concurrency = concurrency

    async def run(self, suite: EvalSuite, target: EvalTarget) -> Scorecard:
        raise NotImplementedError("EvalRunner.run is not implemented yet.")
