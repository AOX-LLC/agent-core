"""The repository's synthetic eval suite runs offline in replay mode."""

import json
import subprocess
import sys
from pathlib import Path

RUN_SCRIPT = Path(__file__).parents[1] / "evals" / "run_triage_eval.py"


def test_triage_eval_replays_and_writes_both_scorecards(tmp_path: Path) -> None:
    scorecard_path = tmp_path / "triage.json"
    completed = subprocess.run(
        [sys.executable, str(RUN_SCRIPT), "--json", str(scorecard_path)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "", "PYTHONPATH": ""},  # no keys, no config overrides
        timeout=120,
    )

    scorecard = json.loads(scorecard_path.read_text())
    assert completed.stdout.startswith("## Eval scorecard: triage")
    assert len(scorecard["results"]) == 10
    assert scorecard["accuracy"] >= 0.7
    assert float(scorecard["cost_total_usd"]) > 0
