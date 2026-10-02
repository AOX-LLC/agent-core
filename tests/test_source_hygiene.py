"""Rules about the source tree itself."""

import ast
import re
from pathlib import Path

import aox_agent_core

PACKAGE_DIR = Path(aox_agent_core.__file__).parent
SOURCE_FILES = sorted(PACKAGE_DIR.rglob("*.py"))

MODEL_ID_PATTERN = re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)", re.IGNORECASE)
SDK_CREDENTIAL_VARIABLES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
}


def test_source_files_were_found() -> None:
    assert len(SOURCE_FILES) > 10


def test_no_model_id_appears_in_code() -> None:
    offenders = [
        f"{path.relative_to(PACKAGE_DIR)}:{line_number}"
        for path in SOURCE_FILES
        for line_number, line in enumerate(path.read_text().splitlines(), start=1)
        if MODEL_ID_PATTERN.search(line)
    ]

    assert offenders == [], "model IDs belong in configuration, not code"


def test_model_ids_live_in_the_packaged_defaults() -> None:
    defaults = (PACKAGE_DIR / "defaults.toml").read_text()

    assert MODEL_ID_PATTERN.search(defaults)


def test_no_code_names_the_sdk_credential_variables() -> None:
    offenders = [
        f"{path.relative_to(PACKAGE_DIR)}:{node.lineno}"
        for path in SOURCE_FILES
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and node.value in SDK_CREDENTIAL_VARIABLES
    ]

    assert offenders == [], "the library reads only AGENT_CORE_ANTHROPIC_API_KEY"
