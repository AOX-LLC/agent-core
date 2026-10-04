"""docs/api.md: its generated outlines are current and cover every public name."""

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCUMENT = ROOT / "docs" / "api.md"
GENERATOR = ROOT / "tools" / "gen_api_docs.py"

# Every area whose whole __all__ the reference documents.
DOCUMENTED_MODULES = [
    "aox_agent_core",
    "aox_agent_core.models",
    "aox_agent_core.replay",
    "aox_agent_core.tracing",
    "aox_agent_core.approvals",
    "aox_agent_core.audit",
    "aox_agent_core.storage",
    "aox_agent_core.evals",
    "aox_agent_core.sync",
]


def generated_block(module: str) -> str:
    name = re.escape(module)
    pattern = rf"<!-- api:begin {name}(?: [^>]*)?-->\n(.*?)<!-- api:end {name} -->"
    match = re.search(pattern, DOCUMENT.read_text(encoding="utf-8"), re.DOTALL)
    assert match is not None, f"docs/api.md has no generated block for {module}"
    return match[1]


def test_generated_outlines_are_up_to_date() -> None:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("module", DOCUMENTED_MODULES)
def test_every_public_name_is_in_the_reference(module: str) -> None:
    block = generated_block(module)

    missing = [
        name for name in importlib.import_module(module).__all__ if f"**`{name}`**" not in block
    ]

    assert not missing, f"{module}: not in docs/api.md: {', '.join(missing)}"
