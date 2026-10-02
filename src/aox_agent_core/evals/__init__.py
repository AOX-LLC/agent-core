"""Run fixture suites and write a scorecard of accuracy, failures, latency and cost."""

from aox_agent_core.evals.runner import EvalRunner, EvalTarget, Scorer
from aox_agent_core.evals.scorecard import render_scorecard_markdown, write_scorecard_json
from aox_agent_core.evals.types import (
    SCORECARD_FORMAT_VERSION,
    CaseResult,
    EvalCase,
    EvalSuite,
    Score,
    Scorecard,
    TargetOutput,
)

__all__ = [
    "SCORECARD_FORMAT_VERSION",
    "CaseResult",
    "EvalCase",
    "EvalRunner",
    "EvalSuite",
    "EvalTarget",
    "Score",
    "Scorecard",
    "Scorer",
    "TargetOutput",
    "render_scorecard_markdown",
    "write_scorecard_json",
]
