"""Writing a scorecard as JSON and Markdown."""

import json
from pathlib import Path

from aox_agent_core.evals.types import CaseResult, Scorecard


def write_scorecard_json(scorecard: Scorecard, path: Path) -> None:
    """Write the scorecard as indented JSON with sorted keys."""
    document = scorecard.model_dump(mode="json")
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_scorecard_markdown(scorecard: Scorecard) -> str:
    """Return a Markdown summary: totals first, then one row per failed case."""
    failures = scorecard.failures()
    passed = len(scorecard.results) - len(failures)
    lines = [
        f"## Eval scorecard: {scorecard.suite}",
        "",
        "| Cases | Passed | Accuracy | p50 latency | p95 latency | Total cost | Cost per case |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| {len(scorecard.results)} | {passed} | {scorecard.accuracy:.1%} "
        f"| {scorecard.latency_p50_ms:.0f} ms | {scorecard.latency_p95_ms:.0f} ms "
        f"| ${scorecard.cost_total_usd:.6f} | ${scorecard.cost_per_case_usd:.6f} |",
        "",
    ]
    if not failures:
        lines.append("No failed cases.")
    else:
        lines += [
            f"### Failed cases ({len(failures)})",
            "",
            "| Case | Why |",
            "| --- | --- |",
            *(f"| {result.case_id} | {_why_failed(result)} |" for result in failures),
        ]
    return "\n".join(lines) + "\n"


def _why_failed(result: CaseResult) -> str:
    """The error if the target raised, else each failed score's detail."""
    if result.error is not None:
        return _cell(result.error)
    if not result.scores:
        return "no scores"
    failed_scores = [score for score in result.scores if not score.passed]
    return _cell(
        "; ".join(f"{score.scorer}: {score.detail or 'failed'}" for score in failed_scores)
    )


def _cell(text: str) -> str:
    """Keep a table cell on one line and its pipes from splitting the row."""
    return text.replace("\n", " ").replace("|", "\\|")
