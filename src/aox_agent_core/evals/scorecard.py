"""Writing a scorecard as JSON and Markdown."""

import json
from pathlib import Path

from aox_agent_core.config import Mode
from aox_agent_core.evals.types import CaseResult, Scorecard


def write_scorecard_json(scorecard: Scorecard, path: Path) -> None:
    """Write the scorecard as indented JSON with sorted keys."""
    document = scorecard.model_dump(mode="json")
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_scorecard_markdown(scorecard: Scorecard) -> str:
    """Return a Markdown summary: the run's mode, totals, then one row per failed case."""
    failures = scorecard.failures()
    passed = len(scorecard.results) - len(failures)
    lines = [
        f"## Eval scorecard: {scorecard.suite}",
        "",
        f"Mode: {_mode_line(scorecard)}",
        "",
        "| Cases | Passed | Accuracy | p50 latency | p95 latency | Total cost | Cost per case |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| {len(scorecard.results)} | {passed} | {scorecard.accuracy:.1%} "
        f"| {_latency(scorecard.latency_p50_ms)} | {_latency(scorecard.latency_p95_ms)} "
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


def _mode_line(scorecard: Scorecard) -> str:
    if scorecard.mode is Mode.REPLAY:
        return (
            "replay. Responses came from recordings, so no latency is reported; "
            "costs are what the recorded calls cost."
        )
    if scorecard.mode is None:
        return "not reported."
    return f"{scorecard.mode.value}."


def _latency(milliseconds: float | None) -> str:
    return f"{milliseconds:.0f} ms" if milliseconds is not None else "n/a"


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
