"""Eval cases, suites, scores and the scorecard."""

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from aox_agent_core._model import FrozenModel
from aox_agent_core.errors import EvalError

SCORECARD_FORMAT_VERSION: Final = 1

CaseId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
UnitInterval = Annotated[float, Field(ge=0, le=1)]
NonNegativeUsd = Annotated[Decimal, Field(ge=0)]
NonNegativeMs = Annotated[float, Field(ge=0)]


class EvalCase(FrozenModel):
    """One input with its expected output, if the scorers need one."""

    id: CaseId
    input: JsonValue
    expected: JsonValue = None
    tags: frozenset[str] = frozenset()


class EvalSuite(FrozenModel):
    """A named set of cases with unique ids."""

    name: CaseId
    cases: Annotated[tuple[EvalCase, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> Self:
        id_counts = Counter(case.id for case in self.cases)
        duplicates = sorted(case_id for case_id, count in id_counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
        return self

    @classmethod
    def from_jsonl(cls, path: Path, *, name: str | None = None) -> Self:
        """Load one EvalCase per line. The suite is named after the file unless given a name.

        Blank lines are skipped. Raises EvalError naming the line of the first
        problem.
        """
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise EvalError(f"Cannot read eval suite {path}: {error}") from error

        cases = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                cases.append(EvalCase.model_validate_json(line))
            except ValidationError as error:
                raise EvalError(f"{path}:{line_number} is not a valid eval case.") from error
        try:
            return cls(name=name if name is not None else path.stem, cases=tuple(cases))
        except ValidationError as error:
            raise EvalError(f"{path} is not a valid eval suite: {error}") from error


class TargetOutput(FrozenModel):
    """What the system under test returns for one case."""

    output: JsonValue
    cost_usd: NonNegativeUsd = Decimal(0)


class Score(FrozenModel):
    """One scorer's judgment of one case."""

    scorer: str
    value: UnitInterval
    passed: bool
    detail: str | None = None


class CaseResult(FrozenModel):
    """Everything recorded for one case. error is set when the target raised."""

    case_id: CaseId
    output: JsonValue = None
    scores: tuple[Score, ...] = ()
    latency_ms: NonNegativeMs
    cost_usd: NonNegativeUsd
    error: str | None = None


class Scorecard(FrozenModel):
    """A suite run's results. Written as JSON and Markdown; its JSON shape is public API.

    accuracy is the share of cases whose every score passed. A case that errored
    or has no scores counts as failed.
    """

    format_version: Literal[1] = SCORECARD_FORMAT_VERSION
    suite: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    results: tuple[CaseResult, ...]
    accuracy: UnitInterval
    latency_p50_ms: NonNegativeMs
    latency_p95_ms: NonNegativeMs
    cost_total_usd: NonNegativeUsd
    cost_per_case_usd: NonNegativeUsd

    def failures(self) -> Sequence[CaseResult]:
        return [
            result
            for result in self.results
            if result.error is not None
            or not result.scores
            or not all(score.passed for score in result.scores)
        ]
