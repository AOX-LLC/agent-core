"""Run fixture suites and write a scorecard of accuracy, failures, latency and cost."""

from aox_agent_core.evals.runner import EvalRunner, EvalTarget, Scorer, model_call_target
from aox_agent_core.evals.scorecard import render_scorecard_markdown, write_scorecard_json
from aox_agent_core.evals.scorers import ExactMatch, FieldMatch
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
    "ExactMatch",
    "FieldMatch",
    "Score",
    "Scorecard",
    "Scorer",
    "TargetOutput",
    "model_call_target",
    "render_scorecard_markdown",
    "write_scorecard_json",
]
