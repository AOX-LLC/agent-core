"""The example runs offline and prints a trace and a cost."""

import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).parents[1] / "examples" / "routed_call.py"


def test_example_prints_a_trace_and_a_cost() -> None:
    environment = {"PATH": "", "PYTHONPATH": ""}  # no API keys, no config overrides
    completed = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        timeout=60,
    )

    assert '"name": "agent_core.model_call"' in completed.stdout
    assert '"agent_core.cost_usd": 0.000405' in completed.stdout
    assert "cost: $0.000405" in completed.stdout
    assert "mode: replay" in completed.stdout
