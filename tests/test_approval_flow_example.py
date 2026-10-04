"""The approval-flow example runs offline: one call, a trace, an approval, a verified log."""

import re
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).parents[1] / "examples" / "approval_flow.py"


def test_example_runs_the_whole_flow_offline() -> None:
    environment = {"PATH": "", "PYTHONPATH": ""}  # no API keys, no config overrides
    completed = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        timeout=60,
    )
    output = completed.stdout

    trace_id = re.search(r"^trace id: ([0-9a-f]{32})$", output, re.MULTILINE)
    assert trace_id is not None
    assert "span: agent_core.model_call" in output
    assert "agent_core.prompt.id = invoices.extract" in output
    assert "agent_core.mode = replay" in output
    # Recorded live: 2069 x $1/M + 59 x $5/M on the small tier (see test_example.py).
    assert "cost: $0.002364" in output

    # The approval moves through its states, in order, and never through self-approval.
    states = [
        "submitted by agent-extract: pending",
        "refused, as it should be:",
        "resolved by user-21: approved",
        "consumed by agent-extract: consumed",
        "second use refused:",
    ]
    positions = [output.index(state) for state in states]
    assert positions == sorted(positions)
    assert "not_human" in output

    assert "audit log verified: 6 records, head seq 6" in output
    assert "FAILED" not in output
