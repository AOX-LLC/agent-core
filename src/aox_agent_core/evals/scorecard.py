"""Writing a scorecard as JSON and Markdown."""

from pathlib import Path

from aox_agent_core.evals.types import Scorecard


def write_scorecard_json(scorecard: Scorecard, path: Path) -> None:
    """Write the scorecard as indented JSON with sorted keys."""
    raise NotImplementedError("write_scorecard_json is not implemented yet.")


def render_scorecard_markdown(scorecard: Scorecard) -> str:
    """Return a Markdown summary: totals first, then one row per failed case."""
    raise NotImplementedError("render_scorecard_markdown is not implemented yet.")
