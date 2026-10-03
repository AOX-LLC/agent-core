"""The example runs offline and prints a trace and a cost."""

import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).parents[1] / "examples" / "routed_call.py"
INVOICE = EXAMPLE.parent / "fixtures" / "invoice-1042.pdf"


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

    # Recorded live: 2,069 input tokens (the prompt plus the one-page PDF) and 59
    # output tokens on the small tier, so 2069 x $1/M + 59 x $5/M.
    assert '"name": "agent_core.model_call"' in completed.stdout
    assert '"agent_core.cost_usd": 0.002364' in completed.stdout
    assert '"agent_core.prompt.id": "invoices.extract"' in completed.stdout
    assert '"agent_core.run_id": "example-run-1"' in completed.stdout
    assert "invoice: INV-1042" in completed.stdout
    assert "cost: $0.002364" in completed.stdout
    assert "mode: replay" in completed.stdout


def test_example_names_the_key_and_path_on_a_miss(tmp_path: Path) -> None:
    edited = tmp_path / "invoice-edited.pdf"
    edited.write_bytes(INVOICE.read_bytes() + b"% edited\n")

    completed = subprocess.run(
        [sys.executable, str(EXAMPLE), "--attachment", str(edited)],
        capture_output=True,
        text=True,
        env={"PATH": "", "PYTHONPATH": ""},
        timeout=60,
    )

    assert completed.returncode == 1
    assert "replay miss: No recording for prompt invoices.extract v1" in completed.stderr
    assert "/examples/replays/prompts/invoices.extract/v1/" in completed.stderr
